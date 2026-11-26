# Posts agent

**Role.** Finds public LinkedIn posts for a topic and reads them with their (preview) comments.

**Inputs.** `keywords`, `limit`.

**Outputs.** Posts (author, profile, date, content, likes, comment count) and their visible comments, written by the
master as `posts-*.csv` and `posts-*-comments.csv`.

**Steps.** `plain_query`, `hashtag_query`, `owner_query` (each tried only while there are not enough posts), then
`read_posts`.

**Brands.** None of its own; uses the shared connector `connectors/linkedin.py`. Logged-out pages show only a
preview of comments.

**Memory store.** `data/memory/posts.sqlite` (posts already seen).
