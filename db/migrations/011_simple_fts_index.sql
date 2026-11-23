-- Exact search deliberately bypasses the English-stemmed bm25_text column and
-- evaluates literal tokens against the passage text. Give that expression its
-- own partial GIN index so exact mode is not a sequential scan.
create index if not exists passages_bm25_simple_gin
  on passages using gin (to_tsvector('simple', text))
  where status = 'current' and level = 'chunk';
