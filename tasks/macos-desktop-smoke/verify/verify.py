from pathlib import Path

from ale_verify import write

p = Path("/Users/admin/output/result.txt")
write({"overall": float(p.is_file() and p.read_text().strip() == "ALE desktop verified")})
