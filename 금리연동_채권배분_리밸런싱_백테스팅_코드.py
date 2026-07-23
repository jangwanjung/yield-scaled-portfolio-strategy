import math
from AlgorithmImports import *


class SCurveYieldScaledMacroRotation(QCAlgorithm):
    """
    ================================================================================
    금리 연동 S-Curve 채권 배분 전략 (혼합형 허용 오차 밴드 적용)
    ================================================================================

    1. 전략 개요
       - 미국 30년 국채 금리(FRED: DGS30) 수준에 따라 채권 목표 비중을 S-Curve로 결정.
       - 고금리 시 채권을 최대 25%까지 채우고, 저금리 시 주식 비중을 확대하여 추세를 추종.
       - 불필요한 슬리피지/수수료 차단을 위해 혼합형 허용 오차 밴드 조건으로 매매 집행.

    2. 기본 배분 (채권 비중 0% 기준)
       - SCHD (상장 전 프록시: VIG) : 50.0%  (배당 슬리브)
       - SPY  (S&P 500)             : 30.0%  (대형주 슬리브)
       - QQQ  (Nasdaq 100)           : 10.0%  (성장주 슬리브)
       - GLD  (Gold)                : 10.0%  (비상관 고정 자산 - 조달 대상 아님)

    3. 채권 비중 산출 및 조달 매커니즘
       - y = FRED DGS30 (30년물 국채 금리, %)
       - 채권 비중 공식: W(y) = Cap * Phi((y - mu) / sigma)
         (Cap = 25.0%, mu = 4.0%, sigma = 0.8%p)
       - 조달 비율 (SCHD : SPY : QQQ = 3 : 2 : 1):
         SCHD 차감 폭 = W * 0.500 (50.0%)
         SPY  차감 폭 = W * 0.333 (33.3%)
         QQQ  차감 폭 = W * 0.167 (16.7%)
         GLD  차감 폭 = 0.000 (10.0% 고정 유지)

    4. 금리대별 포트폴리오 목표 비중 예시
       -------------------------------------------------------------------------
       DGS30 금리   채권 비중   SCHD(배당)   SPY(대형)   QQQ(성장)   GLD(금)
       -------------------------------------------------------------------------
         2.0% 이하     0.0%       50.0%       30.0%       10.0%      10.0%
         3.2%         2.0%       49.0%       29.3%        9.7%      10.0%
         4.0% (중립)  12.5%       43.8%       25.8%        7.9%      10.0%
         4.8%        23.0%       38.5%       22.3%        6.2%      10.0%
         6.0%        24.8%       37.6%       21.7%        5.9%      10.0%
         상한(Max)   25.0%       37.5%       21.7%        5.8%      10.0%
       -------------------------------------------------------------------------

    5. 채권 티커 스플라이싱 (데이터 연속성 확보)
       - TLT (2006년~) -> EDV (2007년~) -> ZROZ (2009년~) 순으로 상장일에 맞춰 스위칭

    6. 리밸런싱 트리거 조건 (매일 장 시작 30분 후 체크)
       - [조건 1] (상대 이탈률 >= 20%) AND (절대 이탈 폭 >= 1.0%p) 동시 충족 자산 발생 시
       - [조건 2] 프록시 ETF -> 실물 ETF 상장 스위칭일 (VIG->SCHD, EDV->ZROZ 등)
    ================================================================================
    """

    # 1. 오차 밴드 및 S-Curve 파라미터
    REL_BAND = 0.20        # 상대 이탈률 (20%)
    ABS_BAND = 0.01        # 절대 이탈 폭 (1.0%p)
    BOND_CAP = 0.25        # 채권 최대 비중 상한 (25%)
    YIELD_MU = 4.0         # S-Curve 중심 금리 (%)
    YIELD_SIGMA = 0.8      # S-Curve 민감도 (%p)

    # 2. 포트폴리오 기본 비중 & 채권 조달 설정 (SCHD : SPY : QQQ = 3 : 2 : 1)
    W_BASE = {'div': 0.50, 'spy': 0.30, 'qqq': 0.10, 'gld': 0.10}
    FUNDING_RATIO = {'div': 3.0, 'spy': 2.0, 'qqq': 1.0}
    BOND_DURATIONS = {'TLT': 17.0, 'EDV': 24.0, 'ZROZ': 27.0}

    # ------------------------------------------------------------------
    # 초기화
    # ------------------------------------------------------------------
    def Initialize(self):
        self.SetStartDate(2006, 6, 1)
        self.SetCash(100000)

        # 자산 심볼 등록 (그룹화 관리)
        self.assets = {
            'spy':  self.AddEquity("SPY", Resolution.Daily).Symbol,
            'qqq':  self.AddEquity("QQQ", Resolution.Daily).Symbol,
            'gld':  self.AddEquity("GLD", Resolution.Daily).Symbol,
            'schd': self.AddEquity("SCHD", Resolution.Daily).Symbol,
            'vig':  self.AddEquity("VIG", Resolution.Daily).Symbol,
            'zroz': self.AddEquity("ZROZ", Resolution.Daily).Symbol,
            'edv':  self.AddEquity("EDV", Resolution.Daily).Symbol,
            'tlt':  self.AddEquity("TLT", Resolution.Daily).Symbol,
        }
        self.dgs30 = self.AddData(Fred, "DGS30", Resolution.Daily).Symbol
        self.SetBenchmark(self.assets['spy'])

        # 조달 비율 정규화 & 채권 비중 상한 산출
        ratio_sum = sum(self.FUNDING_RATIO.values())
        self.funding = {k: v / ratio_sum for k, v in self.FUNDING_RATIO.items()}
        self.max_bond = min(self.W_BASE[k] / f for k, f in self.funding.items() if f > 0)

        self.last_yield = None
        self.active_flags = {'schd': False, 'edv': False, 'zroz': False}

        # 일별 점검 스케줄 등록
        self.Schedule.On(
            self.DateRules.EveryDay(self.assets['spy']),
            self.TimeRules.AfterMarketOpen(self.assets['spy'], 30),
            self.DailyCheck
        )

    # ------------------------------------------------------------------
    # 목표 비중 및 오차 점검 로직
    # ------------------------------------------------------------------
    def _calc_bond_weight(self, yield_val):
        """S-Curve 모델 기반 채권 목표 비중 계산"""
        z = (yield_val - self.YIELD_MU) / self.YIELD_SIGMA
        cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        return min(self.BOND_CAP * cdf, self.max_bond)

    def _get_target_weights(self, yield_val):
        """현재 금리 기준 포트폴리오 목표 비중 계산"""
        bond_w = self._calc_bond_weight(yield_val)
        div_sym, _, bond_sym, _ = self._get_active_assets()

        # 조달 비율에 따른 주식 슬리브 비중 차감
        targets = {
            div_sym:            max(0.0, self.W_BASE['div'] - bond_w * self.funding['div']),
            self.assets['spy']: max(0.0, self.W_BASE['spy'] - bond_w * self.funding['spy']),
            self.assets['qqq']: max(0.0, self.W_BASE['qqq'] - bond_w * self.funding['qqq']),
            bond_sym:           bond_w,
            self.assets['gld']: self.W_BASE['gld']
        }

        # 미사용 프록시 자산들은 0.0%로 기본 세팅
        for sym in self.assets.values():
            targets.setdefault(sym, 0.0)

        return targets

    def _is_band_breached(self, targets):
        """혼합형 오차 밴드 이탈 여부 평가 (선언적 조건 검사)"""
        total_val = self.Portfolio.TotalPortfolioValue
        if total_val <= 0:
            return True

        def is_breached(sym, w_target):
            w_curr = self.Portfolio[sym].HoldingsValue / total_val
            abs_dev = abs(w_curr - w_target)
            rel_dev = (abs_dev / w_target) if w_target > 0 else (1.0 if w_curr > 0 else 0.0)
            return rel_dev >= self.REL_BAND and abs_dev >= self.ABS_BAND

        return any(is_breached(sym, w) for sym, w in targets.items())

    # ------------------------------------------------------------------
    # 상태 점검 및 리밸런싱
    # ------------------------------------------------------------------
    def DailyCheck(self):
        if not self._has_data(self.assets['spy']):
            return

        if self._has_data(self.dgs30):
            self.last_yield = float(self.Securities[self.dgs30].Price)

        if not self.last_yield or self.last_yield <= 0:
            return

        switched = self._check_listings()
        targets = self._get_target_weights(self.last_yield)

        if switched or self._is_band_breached(targets):
            self.Rebalance(targets)

    def Rebalance(self, targets):
        self.SetHoldings([PortfolioTarget(s, w) for s, w in targets.items()])

        div_sym, div_name, bond_sym, bond_name = self._get_active_assets()
        contrib_dur = targets[bond_sym] * self.BOND_DURATIONS[bond_name]

        self.Log(
            f"REBAL {self.Time.date()} | DGS30={self.last_yield:.2f}% | "
            f"{div_name}={targets[div_sym]*100:.1f}% | SPY={targets[self.assets['spy']]*100:.1f}% | "
            f"QQQ={targets[self.assets['qqq']]*100:.1f}% | {bond_name}={targets[bond_sym]*100:.1f}% | "
            f"GLD={targets[self.assets['gld']]*100:.1f}% | 듀레이션={contrib_dur:.1f}년"
        )

    # ------------------------------------------------------------------
    # 유틸리티
    # ------------------------------------------------------------------
    def _get_active_assets(self):
        """현재 상태에 따른 활성 (배당, 채권) 심볼 및 이름 반환"""
        div_sym, div_name = (self.assets['schd'], "SCHD") if self.active_flags['schd'] else (self.assets['vig'], "VIG")

        if self.active_flags['zroz']:
            bond_sym, bond_name = self.assets['zroz'], "ZROZ"
        elif self.active_flags['edv']:
            bond_sym, bond_name = self.assets['edv'], "EDV"
        else:
            bond_sym, bond_name = self.assets['tlt'], "TLT"

        return div_sym, div_name, bond_sym, bond_name

    def _check_listings(self):
        """신규 ETF 상장 스위칭 감지"""
        switched = False
        switch_targets = [
            ('schd', self.assets['schd'], "SCHD 상장 -> VIG 교체"),
            ('edv',  self.assets['edv'],  "EDV 상장 -> TLT 교체"),
            ('zroz', self.assets['zroz'], "ZROZ 상장 -> EDV 교체"),
        ]
        for key, sym, msg in switch_targets:
            if not self.active_flags[key] and self._has_data(sym):
                self.active_flags[key] = True
                switched = True
                self.Log(f"{self.Time.date()} {msg}")
        return switched

    def _has_data(self, sym):
        return self.Securities.ContainsKey(sym) and self.Securities[sym].Price > 0