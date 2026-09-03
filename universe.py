"""종목 유니버스 — 이름 있는 바스켓 또는 사용자 정의.

주의: 정적 현재 구성종목 → 생존편향(survivorship bias) 있음.
상장폐지된 과거 종목이 빠져 있어 모멘텀 성과가 낙관적으로 나올 수 있음.
학습·전략개발용. 엄밀 검증엔 시점별 구성종목 데이터 필요.
"""
import os

UNIVERSES = {
    # 메가캡 15
    "megacap": [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
        "JPM", "JNJ", "V", "WMT", "XOM", "UNH", "LLY", "MA",
    ],
    # 빅테크
    "tech": [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
        "AVGO", "ORCL", "ADBE", "CRM", "AMD", "NFLX", "CSCO", "QCOM",
    ],
    # S&P 100 (OEX) 현재 구성종목 ~100 (위키 기준, BRK.B→BRK-B)
    # ⚠️ 현재 멤버 = 오늘의 승자 → 과거 백테스트에 미래 셀렉션 편향. 절대성과 낙관.
    "sp100": [
        "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AMAT", "AMD", "AMGN", "AMT", "AMZN",
        # BNY = 구 BK (Bank of New York Mellon). 2026-05-21 티커 개명, 상장·CUSIP 불변.
        # 개명 후 2개월간 "BK" 로 남아 매 런 yfinance 3회 헛 재시도 후 스킵 = 후보에서 조용히 이탈.
        # (2026-07-26 전수 생존 점검에서 유일한 DEAD 로 검출 → 교체. BNY 30봉 정상 확인.)
        "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BMY", "BNY", "BRK-B", "C",
        "CAT", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS", "CVX",
        "DE", "DHR", "DIS", "DUK", "EMR", "FDX", "GD", "GE", "GEV", "GILD",
        "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "INTU", "ISRG",
        "JNJ", "JPM", "KO", "LIN", "LLY", "LMT", "LOW", "LRCX", "MA", "MCD",
        "MDLZ", "MDT", "META", "MMM", "MO", "MRK", "MS", "MSFT", "MU", "NEE",
        "NFLX", "NKE", "NOW", "NVDA", "ORCL", "PEP", "PFE", "PG", "PLTR", "PM",
        "QCOM", "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TMO", "TMUS", "TSLA",
        "TXN", "UBER", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WFC", "WMT", "XOM",
    ],
    # 섹터 분산 ~28
    "diversified": [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
        "JPM", "BAC", "V", "MA",
        "JNJ", "UNH", "LLY", "PFE",
        "XOM", "CVX",
        "WMT", "COST", "PG", "KO", "PEP",
        "HD", "MCD", "NKE",
        "DIS", "CAT", "BA",
    ],
    # S&P 500 현재 구성종목 ~500 (위키 기준, BRK.B→BRK-B 등 dash form)
    # ⚠️ 구성종목은 수시 변경 — 주기적 수동 갱신 필요.
    "sp500": [
        "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE", "ADI", "ADM",
        "ADP", "ADSK", "AEE", "AEP", "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM",
        "ALB", "ALGN", "ALL", "ALLE", "AMAT", "AMCR", "AMD", "AME", "AMGN", "AMP",
        "AMT", "AMZN", "ANET", "AON", "AOS", "APA", "APD", "APH", "APO", "APP",
        "APTV", "ARE", "ARES", "ATO", "AVB", "AVGO", "AVY", "AWK", "AXON", "AXP",
        "AZO", "BA", "BAC", "BALL", "BAX", "BBY", "BDX", "BEN", "BF-B", "BG",
        "BIIB", "BKNG", "BKR", "BLDR", "BLK", "BMY", "BNY", "BR", "BRK-B", "BRO",
        "BSX", "BX", "BXP", "C", "CAH", "CARR", "CASY", "CAT", "CB", "CBOE",
        "CBRE", "CCI", "CCL", "CDNS", "CDW", "CEG", "CF", "CFG", "CHD", "CHRW",
        "CHTR", "CI", "CIEN", "CINF", "CL", "CLX", "CMCSA", "CME", "CMG", "CMI",
        "CMS", "CNC", "CNP", "COF", "COHR", "COIN", "COO", "COP", "COR", "COST",
        "CPAY", "CPRT", "CPT", "CRH", "CRL", "CRM", "CRWD", "CSCO", "CSGP", "CSX",
        "CTAS", "CTSH", "CTVA", "CVNA", "CVS", "CVX", "D", "DAL", "DASH", "DD",
        "DDOG", "DE", "DECK", "DELL", "DG", "DGX", "DHI", "DHR", "DIS", "DLR",
        "DLTR", "DOC", "DOV", "DOW", "DPZ", "DRI", "DTE", "DUK", "DVA", "DVN",
        "DXCM", "EA", "EBAY", "ECHO", "ECL", "ED", "EFX", "EG", "EIX", "EL",
        "ELV", "EME", "EMR", "EOG", "EQIX", "EQR", "EQT", "ERIE", "ES", "ESS",
        "ETN", "ETR", "EVRG", "EW", "EXC", "EXE", "EXPD", "EXPE", "EXR", "F",
        "FANG", "FAST", "FCX", "FDS", "FDX", "FDXF", "FE", "FFIV", "FICO", "FIS",
        "FISV", "FITB", "FIX", "FLEX", "FOX", "FOXA", "FRT", "FSLR", "FTNT", "FTV",
        "GD", "GDDY", "GE", "GEHC", "GEN", "GEV", "GILD", "GIS", "GL", "GLW",
        "GM", "GNRC", "GOOG", "GOOGL", "GPC", "GPN", "GRMN", "GS", "GWW", "HAL",
        "HAS", "HBAN", "HCA", "HD", "HIG", "HII", "HLT", "HON", "HONA", "HOOD",
        "HPE", "HPQ", "HRL", "HSIC", "HST", "HSY", "HUBB", "HUM", "HWM", "IBKR",
        "IBM", "ICE", "IDXX", "IEX", "IFF", "INCY", "INTC", "INTU", "INVH", "IP",
        "IQV", "IR", "IRM", "ISRG", "IT", "ITW", "IVZ", "J", "JBHT", "JBL",
        "JCI", "JKHY", "JNJ", "JPM", "KDP", "KEY", "KEYS", "KHC", "KIM", "KKR",
        "KLAC", "KMB", "KMI", "KO", "KR", "KVUE", "L", "LDOS", "LEN", "LH",
        "LHX", "LII", "LIN", "LITE", "LLY", "LMT", "LNT", "LOW", "LRCX", "LULU",
        "LUV", "LVS", "LYB", "LYV", "MA", "MAA", "MAR", "MAS", "MCD", "MCHP",
        "MCK", "MCO", "MDLZ", "MDT", "MET", "META", "MGM", "MKC", "MLM", "MMM",
        "MNST", "MO", "MOS", "MPC", "MPWR", "MRK", "MRNA", "MRSH", "MRVL", "MS",
        "MSCI", "MSFT", "MSI", "MTB", "MTD", "MU", "NCLH", "NDAQ", "NDSN", "NEE",
        "NEM", "NFLX", "NI", "NKE", "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS",
        "NUE", "NVDA", "NVR", "NWS", "NWSA", "NXPI", "O", "ODFL", "OKE", "OMC",
        "ON", "ORCL", "ORLY", "OTIS", "OXY", "PANW", "PAYX", "PCAR", "PCG", "PEG",
        "PEP", "PFE", "PFG", "PG", "PGR", "PH", "PHM", "PKG", "PLD", "PLTR",
        "PM", "PNC", "PNR", "PNW", "PODD", "PPG", "PPL", "PRU", "PSA", "PSKY",
        "PSX", "PTC", "PWR", "PYPL", "Q", "QCOM", "RCL", "REG", "REGN", "RF",
        "RJF", "RL", "RMD", "ROK", "ROL", "ROP", "ROST", "RSG", "RTX", "RVTY",
        "SBAC", "SBUX", "SCHW", "SHW", "SJM", "SLB", "SMCI", "SNA", "SNDK", "SNPS",
        "SO", "SOLV", "SPG", "SPGI", "SRE", "STE", "STLD", "STT", "STX", "STZ",
        "SW", "SWK", "SWKS", "SYF", "SYK", "SYY", "T", "TAP", "TDG", "TDY",
        "TECH", "TEL", "TER", "TFC", "TGT", "TJX", "TKO", "TMO", "TMUS", "TPL",
        "TPR", "TRGP", "TRMB", "TROW", "TRV", "TSCO", "TSLA", "TSN", "TT", "TTD",
        "TTWO", "TXN", "TXT", "TYL", "UAL", "UBER", "UDR", "UHS", "ULTA", "UNH",
        "UNP", "UPS", "URI", "USB", "V", "VEEV", "VICI", "VLO", "VLTO", "VMC",
        "VRSK", "VRSN", "VRT", "VRTX", "VST", "VTR", "VTRS", "VZ", "WAB", "WAT",
        "WBD", "WDAY", "WDC", "WEC", "WELL", "WFC", "WM", "WMB", "WMT", "WRB",
        "WSM", "WST", "WTW", "WY", "WYNN", "XEL", "XOM", "XYL", "XYZ", "YUM",
        "ZBH", "ZBRA", "ZTS",
    ],
    # 캐시우드형 파괴성장 ~45 (혁신·고성장 테마, 스테이플·금융 제외)
    "growth": [
        "TSLA", "NVDA", "AMD", "PLTR", "SHOP", "COIN", "CRWD", "NET", "SNOW",
        "DDOG", "RBLX", "ROKU", "XYZ", "MELI", "SE", "ABNB", "U", "TWLO",
        "ZS", "MDB", "DOCN", "PATH", "AI", "SOFI", "HOOD", "DKNG",
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "ORCL", "ADBE", "CRM",
        "NFLX", "QCOM", "MU", "ARM", "SMCI", "MRVL", "APP", "UBER",
        "DASH", "ANET",
    ],
}


def get_universe(spec: str) -> list:
    """이름(megacap/tech/diversified) | CSV/TXT 경로 | 콤마구분 문자열."""
    if spec in UNIVERSES:
        return UNIVERSES[spec]
    if os.path.exists(spec):
        with open(spec, encoding="utf-8") as f:
            text = f.read()
        seps = text.replace("\n", ",").replace("\t", ",")
        return list(dict.fromkeys(t.strip().upper() for t in seps.split(",") if t.strip()))
    return list(dict.fromkeys(t.strip().upper() for t in spec.split(",") if t.strip()))
