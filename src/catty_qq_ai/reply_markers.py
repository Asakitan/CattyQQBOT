REPLY_SPLIT_MARKER = "<<<CATTY_REPLY_SPLIT>>>"
NO_REPLY_MARKER = "<<<CATTY_NO_REPLY>>>"
EMOJI_QUERY_PREFIX = "<<<CATTY_EMOJI_QUERY:"
EMOJI_QUERY_SUFFIX = ">>>"
TRAILING_CHAT_PUNCTUATION = " \t\r\n。！？!?；;，,、：:…."


def extract_emoji_query(reply: str) -> tuple[str, str]:
    clean = reply
    selected_query = ""

    while True:
        start = clean.find(EMOJI_QUERY_PREFIX)
        if start < 0:
            return clean.strip(), selected_query

        query_start = start + len(EMOJI_QUERY_PREFIX)
        end = clean.find(EMOJI_QUERY_SUFFIX, query_start)
        if end >= 0:
            query = clean[query_start:end].strip()
            clean = clean[:start] + clean[end + len(EMOJI_QUERY_SUFFIX) :]
        else:
            line_end = clean.find("\n", query_start)
            if line_end >= 0:
                query = clean[query_start:line_end].strip()
                clean = clean[:start] + clean[line_end + 1 :]
            else:
                query = clean[query_start:].strip()
                clean = clean[:start]

        if query and not selected_query:
            selected_query = query
