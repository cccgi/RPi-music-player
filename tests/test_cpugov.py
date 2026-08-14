"""player/cpugov.py: CPU governor control for the idle low-power feature.

Covers:
  - read_governor()/set_governor() operate correctly across multiple cores.
  - set_governor() only reports success when EVERY core's write succeeded,
    not silently claiming success on a partial write.
  - Both degrade to a harmless no-op (None / False) rather than crashing
    when cpufreq isn't present at all -- this sandbox, for instance.
"""
import _bootstrap  # noqa: F401
import tempfile
from pathlib import Path

from player.config import setup_logging
from player import cpugov
setup_logging("CRITICAL")

fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  {'OK ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")


def make_fake_cpu_root(tmp, cores=4, readonly=()):
    """Mimics /sys/devices/system/cpu/cpuN/cpufreq/scaling_governor for
    ``cores`` cores, each starting at 'ondemand'. Cores listed in
    ``readonly`` get their file chmod'd 0444, reproducing exactly what a
    missing tmpfiles.d permission fix looks like on the real box: the path
    exists (the kernel always creates it) but writing to it as the
    unprivileged rpi user raises PermissionError."""
    root = Path(tmp)
    for n in range(cores):
        d = root / f"cpu{n}" / "cpufreq"
        d.mkdir(parents=True)
        path = d / "scaling_governor"
        path.write_text("ondemand")
        if n in readonly:
            path.chmod(0o444)
    return root


print("=== read_governor: no cpufreq present at all -> None, no crash ===")
with tempfile.TemporaryDirectory() as tmp:
    cpugov._CPU_ROOT = Path(tmp)  # empty -- nothing under it
    check("no cores at all", cpugov.read_governor(), None)
    check("set_governor also just returns False, does not raise",
          cpugov.set_governor("powersave"), False)

print("\n=== read_governor / set_governor: normal 4-core board ===")
with tempfile.TemporaryDirectory() as tmp:
    root = make_fake_cpu_root(tmp, cores=4)
    cpugov._CPU_ROOT = root
    check("reads cpu0's governor", cpugov.read_governor(), "ondemand")

    check("set_governor succeeds", cpugov.set_governor("powersave"), True)
    for n in range(4):
        got = (root / f"cpu{n}" / "cpufreq" / "scaling_governor").read_text().strip()
        check(f"cpu{n} actually set to powersave", got, "powersave")
    check("read_governor reflects the new value", cpugov.read_governor(), "powersave")

print("\n=== set_governor: a partial write (one core's tmpfiles.d fix "
      "missing, so its write is denied) is reported as failure, not "
      "silently ignored ===")
with tempfile.TemporaryDirectory() as tmp:
    root = make_fake_cpu_root(tmp, cores=4, readonly=(2,))
    cpugov._CPU_ROOT = root
    try:
        ok = cpugov.set_governor("powersave")
        check("overall result is False -- cpu2's write was denied", ok, False)
        check("cpu0 still got its write (best-effort on the others)",
              (root / "cpu0" / "cpufreq" / "scaling_governor").read_text().strip(),
              "powersave")
        check("cpu2 untouched, still the original value",
              (root / "cpu2" / "cpufreq" / "scaling_governor").read_text().strip(),
              "ondemand")
    finally:
        (root / "cpu2" / "cpufreq" / "scaling_governor").chmod(0o644)  # let tempdir cleanup unlink it

print()
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
print("All cpugov tests passed.")
