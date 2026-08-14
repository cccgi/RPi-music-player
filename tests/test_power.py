"""player/power.py: Pi board power draw for the Video page's power tile.

Covers:
  - parse_pmic_adc() correctly pairs current/voltage rails by their shared
    <NAME> prefix (NOT the parenthesised ADC channel index, which does not
    line up between the current and voltage lists -- see the module
    docstring), computes total watts, and skips a rail that only has a
    current reading with no matching voltage rather than guessing.
  - read_pi_current_ma() converts watts to a 5V-equivalent mA figure, and
    returns None (never a fabricated number) on a missing binary, a
    non-zero exit, a timeout, or unparseable output.
"""
import _bootstrap  # noqa: F401
import subprocess

from player.config import setup_logging
from player import power
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


# Real `vcgencmd pmic_read_adc` shape (see power.py's docstring for why the
# current(N)/volt(N) indices do NOT correspond to the same rail -- matching
# has to go by the <NAME> prefix instead). Deliberately includes one
# current-only rail (NO_VOLT_A) with no matching voltage line, to prove it
# gets skipped rather than guessed at.
SAMPLE = """\
 3V7_WL_SW_A current(0)=0.02342232A
   3V3_SYS_A current(1)=0.15419700A
  VDD_CORE_A current(7)=1.12518000A
    NO_VOLT_A current(9)=0.50000000A
 3V7_WL_SW_V volt(8)=3.71740800V
   3V3_SYS_V volt(9)=3.31745700V
  VDD_CORE_V volt(15)=0.89316150V
"""

print("=== parse_pmic_adc: pairs rails by name, sums matched-only power ===")
watts = power.parse_pmic_adc(SAMPLE)
expected = (0.02342232 * 3.71740800) + (0.15419700 * 3.31745700) + (1.12518000 * 0.89316150)
check("total watts from the 3 matched rails only (NO_VOLT_A skipped)",
      round(watts, 6), round(expected, 6))

print("\n=== parse_pmic_adc: no current rails at all -> None ===")
check("empty input", power.parse_pmic_adc(""), None)
check("garbage input", power.parse_pmic_adc("not a real vcgencmd output\n"), None)


class FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def fake_run(ok=True, stdout=SAMPLE, raises=None):
    def _run(*args, **kwargs):
        if raises is not None:
            raise raises
        return FakeCompleted(returncode=0 if ok else 1, stdout=stdout)
    return _run


print("\n=== read_pi_current_ma: converts watts -> 5V-equivalent mA ===")
real_run = subprocess.run
subprocess.run = fake_run()
try:
    ma = power.read_pi_current_ma()
finally:
    subprocess.run = real_run
check("5V-equivalent mA matches watts/5.0*1000",
      round(ma, 3) if ma is not None else None,
      round((expected / 5.0) * 1000.0, 3))

print("\n=== read_pi_current_ma: failure modes never fabricate a number ===")
subprocess.run = fake_run(ok=False)
try:
    check("non-zero exit -> None", power.read_pi_current_ma(), None)
finally:
    subprocess.run = real_run

subprocess.run = fake_run(raises=FileNotFoundError("no vcgencmd"))
try:
    check("binary missing -> None", power.read_pi_current_ma(), None)
finally:
    subprocess.run = real_run

subprocess.run = fake_run(raises=subprocess.TimeoutExpired(cmd="vcgencmd", timeout=3.0))
try:
    check("timeout -> None", power.read_pi_current_ma(), None)
finally:
    subprocess.run = real_run

subprocess.run = fake_run(stdout="garbage, no rails here\n")
try:
    check("unparseable output -> None", power.read_pi_current_ma(), None)
finally:
    subprocess.run = real_run

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All power tests passed.")
