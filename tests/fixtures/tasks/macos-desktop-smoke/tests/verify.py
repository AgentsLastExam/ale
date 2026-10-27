from pathlib import Path

from ale_verify import write

p = Path("/Users/admin/output/result.txt")
write({"reward": float(p.is_file() and p.read_text().strip() == "ALE desktop verified")})
