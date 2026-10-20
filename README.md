# preflight-charts

Aeronautical chart data for the [Preflight](https://github.com/jesselarson84/Preflight) drone app,
rebuilt every week from the FAA's own public-domain sources:

| Layer | Source |
| --- | --- |
| Class B, C, D, E airspace, Mode C veils | FAA AIS open data, Class Airspace |
| Prohibited, restricted, MOA, warning, alert areas | FAA AIS open data, Special Use Airspace |
| LAANC grid (UAS facility map) | FAA AIS open data, UAS Facility Map |
| Airports, runways, frequencies, navaids | FAA NASR 28-day subscription |
| Obstacles 200 ft and taller | FAA Digital Obstacle File |

`scripts/build_chart.py` turns those into GeoJSON layers, and the **Publish chart** workflow packs
them into one [PMTiles](https://github.com/protomaps/PMTiles) file and publishes it as a release
with a `manifest.json`. The app reads
`releases/latest/download/manifest.json` to find the current file and streams only the tiles it
needs.

For planning only, not navigation.
