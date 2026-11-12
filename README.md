# Release Radar

A self-hosted release feed for movies and series, built on the TMDB API.

- **Released** and **Upcoming** tabs, each a chronological feed
- Per-user tracking: cinemas, digital/VOD, and any streaming service TMDB knows (Netflix, Disney+, ...)
- Regions: Germany and United States
- Filters: movies/series, genre, source, region, title search; group by day, week or month; sort by date, popularity, title or rating (all remembered per user)
- User accounts with login; admins create and manage users

## Install

1. Copy `compose.yaml` to the server.
2. Fill in `TMDB_TOKEN` and `ADMIN_PASSWORD`.
3. `docker compose up -d`
4. In Nginx Proxy Manager add a proxy host: `tmdb.greeed.de` -> `http://<server-ip>:8097`, with an SSL certificate and "Force SSL".

Log in with `ADMIN_USER` / `ADMIN_PASSWORD`. These two variables only matter on the very first start; afterwards manage accounts in the **Users** tab. The first data sync takes a few minutes.

Data lives in the `release-radar-data` volume (one SQLite file). Update with `docker compose pull && docker compose up -d`.

## How dates are determined

| Source | Date used |
|---|---|
| In theaters | TMDB theatrical release date for the region |
| Digital / VOD | TMDB digital release date for the region |
| Streaming service, series | Episode air dates of shows on that service (or ordered by it) |
| Streaming service, movies | Regional digital release date, or the premiere date for titles that skipped cinemas |

Limits that come from TMDB's data:

- TMDB does not record *when* a title was added to a service. Older catalogue films arriving on a service are not detected, and a cinema film without a digital date is skipped.
- A movie is linked to a service only once it is available there, so upcoming streaming *movies* are sparse. Upcoming *series* work well.
- Episode air dates are global, not per country.
- Daily talk, news and soap shows are excluded.

## Development

    pip install -r requirements.txt
    DATA_DIR=./data TMDB_TOKEN=... ADMIN_PASSWORD=... uvicorn app.main:app --port 8097
