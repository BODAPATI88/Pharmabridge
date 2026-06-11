PharmaBridge Netmeds API Discovery Notes

Status

Date: 2026-06-11

Status: API discovered and validated.

Legacy HTML scraper endpoint is no longer valid:

https://www.netmeds.com/catalogsearch/result?q=<medicine>

Current worker receives:

HTTP 404 Not Found

The existing BeautifulSoup scraper is obsolete.

---

Discovery Outcome

Netmeds exposes a product search API returning JSON.

Response structure:

{
  "filters": [],
  "items": [],
  "user_data": {},
  "page": {},
  "sort_on": "",
  "meta": {}
}

Primary payload:

data["items"]

---

Example Search

Medicine:

paracetamol

Result count:

12 products

Example products:

Paracetamol 500mg Tablet 10'S
Dolo 650 Tablet 15's
Paracip 500mg Tablet 10'S
CALPOL 650 + Tablet 15's
CROCIN ADVANCE Tablet 20's

---

Important Product Fields

Product name:

item["name"]

Example:

Paracetamol 500mg Tablet 10'S

Slug:

item["slug"]

Example:

paracetamol-500mg-tablet-10s-m1v2mv-8520913

Product URL:

"https://www.netmeds.com/prescriptions/" + item["slug"]

Example:

https://www.netmeds.com/prescriptions/paracetamol-500mg-tablet-10s-m1v2mv-8520913

---

Pricing Fields

Selling price:

item["price"]["effective"]["min"]

MRP:

item["price"]["marked"]["min"]

Example:

Price: 6.76
MRP:   9.65

---

Availability

Stock availability:

item["sellable"]

Example:

True

---

Image Fields

Primary image:

item["medias"][0]["url"]

Example:

https://cdn.pixelbin.io/...

---

Manufacturer Information

Found within:

item["attributes"]

Useful fields:

manufacturername
marketername
genericname
genericnamewithdosage
ingredients

Example:

manufacturername:
Glaxosmithkline Pharmaceuticals Ltd

genericname:
Paracetamol

---

Fields Needed By VendorMedicineResult

Mapping target:

VendorMedicineResult

Required mappings:

brand_name
generic_name
manufacturer
price_per_unit
price_per_strip
strip_size
mrp
discount_pct
in_stock
requires_rx
product_url
image_url

---

Migration Plan

Remove:

_parse_html()
BeautifulSoup
catalogsearch/result

Replace with:

HTTP JSON API request
Parse data["items"][0]
Map fields directly into VendorMedicineResult

---

Validation Completed

Confirmed working:

- Product search
- Product name extraction
- Product URL generation
- Price extraction
- MRP extraction
- Availability extraction
- Image extraction

Pending:

- Worker implementation
- Integration testing
- Smart Split validation
- Production rollout
