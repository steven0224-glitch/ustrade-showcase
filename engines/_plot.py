"""matplotlib 공통 설정 — Agg 백엔드 + 한글 폰트(Malgun Gothic)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.family"] = "Malgun Gothic"   # Windows 기본 한글 폰트
plt.rcParams["axes.unicode_minus"] = False       # 마이너스 부호 깨짐 방지
