from ale_verify import checks, write

check = checks.text_equals("/home/user/output/result.txt", "hello\n")
write({"overall": check["score"], "content": check})
