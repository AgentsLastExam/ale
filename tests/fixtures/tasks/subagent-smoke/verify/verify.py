from ale_verify import checks, write

# A trailing newline from a shell write is not a different answer.
child = checks.text_regex("/home/user/output/child.txt", r"\ASUBAGENT_MARKER_7F3A\s*\Z")
write({"reward": child["score"], "child": child})
