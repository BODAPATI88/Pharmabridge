Netmeds Worker Migration Status
Overview
The original Netmeds implementation used HTML scraping with BeautifulSoup against:
https://www.netmeds.com/catalogsearch/result?q=⁠�
This endpoint no longer works and now returns HTTP 404.
As a result, the current live Netmeds worker cannot retrieve medicine data and returns zero results.
Current Status
Completed
Netmeds worker created
Mock mode implemented
Live mode tested
Discovery script created
API investigation performed
JSON response structure validated
Discovery notes documented
Backup worker preserved
Pre-live docker-compose configuration preserved
Confirmed Findings
Legacy HTML scraping endpoint:
https://www.netmeds.com/catalogsearch/result?q=⁠�
Result:
HTTP 404 Not Found
The BeautifulSoup implementation in worker_netmeds.py is therefore obsolete.
Evidence Collected
Validated JSON response fields:
items
slug
name
sellable
price.effective.min
price.marked.min
Example observed product:
Paracetamol 500mg Tablet 10'S
Example observed fields:
slug
effective price
marked price
sellable status
This confirms Netmeds currently exposes medicine data through a JSON API.
Current Blocker
The following information was not preserved during discovery:
API endpoint URL
Required request headers
Query parameters
Authentication requirements (if any)
Response pagination behavior
Without this information the worker cannot be migrated from HTML scraping to API mode.
Repository Artifacts
Documentation:
docs/netmeds-api-notes.md
Discovery utility:
scripts/discover_netmeds_api.py
Backup worker:
workers/worker_netmeds.py.bak
Pre-live compose file:
docker-compose.yml.pre-live-netmeds
Next Actions
Re-run Netmeds API discovery.
Capture exact API endpoint.
Capture required headers.
Capture query parameters.
Validate JSON response structure.
Replace _bs4_scrape() with _api_scrape().
Rebuild worker container.
Execute end-to-end validation.
Platform Status
Vendor Status:
1mg: MOCK MODE
PharmEasy: MOCK MODE
Netmeds: LIVE MODE FAILED
Priority remains achieving the first fully live vendor integration before expanding additional vendor functionality.
Last Updated: 2026-06-11 :::
