# Discovery agent

**Role.** Finds candidate companies for an industry in a US city.

**Inputs.** `industry`, `city`, `state`, `limit`.

**Outputs.** `run()` returns `(companies, per)`: a merged, de-duplicated list of company dicts plus a per-source
list. After each run `agent.source_errors` holds per-source failure counts (e.g. `{"osm": 2, "web": 1}`); an empty
dict means every source answered.

**Steps** (reorderable by learned yield, stop once there are enough candidates):
1. `npi`: NPI Registry (US healthcare, includes the authorized official).
2. `osm`: OpenStreetMap (Nominatim + Overpass).
3. `web`: DuckDuckGo web search (US region, directories filtered).

**Brands** (`brands/`): `npi_registry.py`, `openstreetmap.py`, `web_search.py`; shared helper `_common.py`.
Uses the shared `connectors/duckduckgo.py`.

**Failure handling.** Nothing fails silently: `[discovery] osm FAILED (<reason>)`, `osm: 0 results for <tag>` and
`web: N search queries returned no results` are printed, and counted in `source_errors`. A source that errored is not
counted as a failed try in the learned step stats (so it is never skipped as "useless" because of an outage).
- Overpass: 2 rounds over 3 mirrors (overpass-api.de, kumi.systems, private.coffee) with backoff; a mirror that just
  failed is skipped for 90 s. Failed, error-remark or empty answers are deleted from the HTTP cache (`fetch()` would
  otherwise keep any HTTP 200 for 14 days). If Overpass is down, a Nominatim place search is used for simple tags.
- OSM results are de-duplicated per object and filtered to the requested state.

**Manufacturing.** OSM is sparse for it, so `osm` uses a padded bounding box (metro, not just the city polygon) and
several tags (`industrial=factory`, `man_made=works`, craft/industrial sub-types, and named industrial buildings /
`office=company` with a website or an industrial-sounding name); breweries, wineries, distilleries and shops are
dropped. `web_search.QUERY_VARIANTS` has 18 sub-niche queries, followed by nearby-city queries (`NEARBY`) when the
pool is still small.

**Memory store.** `data/memory/discovery.sqlite` (source yield per industry).
