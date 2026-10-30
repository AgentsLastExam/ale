from ale_verify import checks, write

check = checks.text_equals("/home/user/output/result.txt", "hello\n")
write({"reward": check["score"], "content": check})
