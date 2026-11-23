from pathlib import Path

from orbit_verify import write

path = Path("/Users/tester/output/result.txt")
lines = path.read_text().splitlines() if path.is_file() else []
write({"reward": float(lines == ["user=tester", "admin=no", "sudo=denied", "build=built"])})
