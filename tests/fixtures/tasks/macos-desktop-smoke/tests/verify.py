from pathlib import Path

from orbit_verify import write

p = Path("/Users/admin/output/result.txt")
write({"reward": float(p.is_file() and p.read_text().strip() == "Orbit desktop verified")})
