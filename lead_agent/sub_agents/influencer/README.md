# Influencer agent

**Role.** Standalone tracker of public LinkedIn posts for a list of profiles (logged out, free). It is not driven by
the Master Agent; run it with `python -m lead_agent influencer <file> [--limit N] [--no-post-pages] [--days 60] [--apify]`.

**Inputs.** An Excel/CSV with LinkedIn profile URLs (any column holding `linkedin.com/in/` links).

**Outputs.** An Excel workbook with sheets `posts` (one row per post, newest first per person) and `summary`.

**Steps.** Read each public profile, collect the posts/articles LinkedIn shows logged out, search for further posts,
then open each post for date, likes and comment count.

**Brands.** None of its own; uses connectors `linkedin` and `duckduckgo`.

**Memory store.** None.
