from pathlib import Path

from ale_verify import write

path = Path("/Users/tester/output/result.txt")
lines = path.read_text().splitlines() if path.is_file() else []
write({"reward": float(lines == ["user=tester", "admin=no", "sudo=denied", "build=built"])})
