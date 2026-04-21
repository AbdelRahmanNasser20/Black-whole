# Black Whole Liquidation — Full Automation Blueprint

> **Purpose:** End-to-end automation pipeline for extracting GovDeals listings, removing watermarks, and creating Facebook Marketplace + eBay listings.
> **How to use:** Paste a GovDeals URL into any Claude chat and say "run the gov deals skill" — Claude follows this blueprint automatically.

---

## Pipeline Overview

```
GovDeals URL
    │
    ▼
┌──────────────────────┐
│  PHASE 1: EXTRACT    │  Navigate → scrape metadata → get image URLs
│  (Automated)         │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  PHASE 2: DOWNLOAD   │  curl commands → save to named folder
│  (User pastes cmds)  │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  PHASE 3: DEWATERMARK│  Open dewatermark.ai → user drags images
│  (User drag-drop)    │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  PHASE 4: LIST ON FB │  Auto-fill title, price, category, condition,
│  (Automated)         │  description (dynamic location), settings
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  PHASE 5: LIST EBAY  │  Auto-fill via bulk sell tool
│  (Automated)         │
└──────────────────────┘
```

---

## PHASE 1: EXTRACT METADATA FROM GOVDEALS

### Input
A GovDeals URL like: `https://www.govdeals.com/en/asset/{seller_id}/{lot_id}`

### Step 1.1 — Navigate
```
Claude in Chrome:navigate → url: <govdeals URL>
Wait 4 seconds for page + lightGallery carousel to load
```

### Step 1.2 — Extract Metadata (Single JS Call)
```javascript
const title = document.querySelector('h1')?.textContent?.trim() || 'Unknown';
const allLinks = Array.from(document.querySelectorAll('a'));
const locLink = allLinks.find(a => {
  const t = a.textContent;
  return (t.includes('USA') || t.match(/,\s*[A-Z][a-z]+,/) || t.match(/,\s*[A-Z]{2}/))
    && !t.includes('Search') && !t.includes('View');
});
const location = locLink?.textContent?.trim() || 'Unknown';
const city = location.split(',')[0].trim();
const bodyText = document.body.innerText;
const qtyMatch = bodyText.match(/\((\d{1,5})\)/);
const quantity = qtyMatch ? qtyMatch[1] : title.match(/(\d+)/)?.[1] || 'NA';
const folder = title.replace(/[^a-zA-Z0-9\s]/g, '').replace(/\s+/g, '_')
  + '_' + city.replace(/\s+/g, '_') + '_' + quantity;
```

**Output variables:**
| Variable | Example | Used In |
|----------|---------|---------|
| `title` | "Lot of 299 Tan Metal Folding Chairs" | FB title, eBay title |
| `location` | "Nellis Air Force Base, Nevada, USA" | FB description, eBay location |
| `city` | "Nellis Air Force Base" | Folder name |
| `quantity` | "299" | Description, folder name |
| `folder` | "Tan_Metal_Folding_Chairs_Nellis_Air_Force_Base_299" | File storage path |

### Step 1.3 — Load All Carousel Images
Click the carousel next arrow (right side of image) repeatedly until all slides load.
Then extract URLs:

```javascript
// Click carousel right arrow 5-8 times first, then:
const lgImgs = document.querySelectorAll('.lg-item img');
const thumbImgs = document.querySelectorAll('img[src*="assets/photos"]');
const urls = [...new Set(
  [...Array.from(lgImgs), ...Array.from(thumbImgs)]
    .map(i => i.src.split('?')[0])
)].filter(u => !u.includes('youtube'));
```

**Verify:** `urls.length` should match the "X / Y" counter in the carousel header.

### GovDeals Page Structure
```
URL:          https://www.govdeals.com/en/asset/{seller_id}/{lot_id}
Image CDN:    https://webassets.lqdt1.com/assets/photos/{lot_id}/{lot_id}_{seller_id}_{uuid}.jpg
Gallery:      lightGallery (.lg-container → .lg-item → img)
Thumbnails:   img[src*="assets/photos"]
Title:        <h1>
Location:     <a> link near "Location:" label
Description:  Text block below the details table
```

---

## PHASE 2: DOWNLOAD IMAGES

### Why curl (not JS downloads)
JS browser downloads via `<a>.click()` are **unreliable** — Chrome silently blocks them.
`curl` always works.

### Generate curl Commands (JS on listing page)
```javascript
const base = '/Users/abdelnasser/Desktop/Banquet chiars Pictures';
const cmds = [
  `mkdir -p "${base}/${folder}"`,
  ...urls.map((u, i) => {
    const ext = u.match(/\.(jpe?g|png)/i)?.[0] || '.jpg';
    return `curl -o "${base}/${folder}/${folder}_${i+1}${ext}" "${u}"`;
  })
];
cmds.join('\n');
```

### Output to User
Present the full block of commands for the user to paste into Terminal.

### Folder Convention
```
/Users/abdelnasser/Desktop/Banquet chiars Pictures/
  └── {Title}_{City}_{Quantity}/
        ├── {Title}_{City}_{Quantity}_1.jpg
        ├── {Title}_{City}_{Quantity}_2.jpeg
        └── ...
```

---

## PHASE 3: REMOVE WATERMARKS (dewatermark.ai)

### Why dewatermark.ai
- OpenCV inpainting scripts failed on GovDeals' semi-transparent watermarks
- dewatermark.ai uses AI-based removal — works perfectly
- User has a paid subscription (HD downloads, batch mode)

### Automation Steps
1. Open `https://dewatermark.ai/` in a browser tab (automated)
2. **User drags images** from the folder onto the upload area (manual — Chrome security blocks file_upload to 3rd party sites)
3. User downloads cleaned versions back to the same folder

### Cannot Automate (Chrome Security)
- `file_upload` tool → "Not allowed" on dewatermark.ai and Facebook
- `fetch()` cross-origin → blocked by CORS
- Clipboard API → "Document not focused"
- Canvas `toBlob()` → "Tainted canvas"

---

## PHASE 4: CREATE FACEBOOK MARKETPLACE LISTING

### Step 4.1 — Navigate to Create Listing
```
Claude in Chrome:navigate → url: https://www.facebook.com/marketplace/create/item
Wait 4 seconds
```

### Step 4.2 — Fill Page 1 (Item Details)

| Field | Value | How |
|-------|-------|-----|
| Title | `{title} - {style description}` | Click field → type |
| Price | `$20` (default per-chair price) | Click field → type |
| Category | "Dining Furniture Sets" | Click dropdown → type "Furniture" → select |
| Condition | "Used - Good" | Click dropdown → select |
| Description | See template below | Click field → type |

### ⚠️ DESCRIPTION TEMPLATE (Dynamic Location!)

```
Location: {LOCATION}

Large Quantity of {CHAIR_TYPE} Available
{DIMENSIONS_IF_AVAILABLE}

Update: About {ESTIMATED_CHAIRS_REMAINING} chairs left in current inventory

Great for churches, banquet halls, community centers, and large events.

Please include in your message:
1. Chair type / photo number
2. Quantity needed
3. Pickup or delivery
4. Your city / zip code for delivery quotes

Delivery is available for a fee based on distance and order size.

Bulk orders welcome. First come, first served.
```

**Variable substitution:**

| Variable | Source | Example |
|----------|--------|---------|
| `{LOCATION}` | GovDeals location link | "Nellis Air Force Base, Nevada, USA" |
| `{CHAIR_TYPE}` | From title | "Tan Metal Folding Chairs" |
| `{DIMENSIONS_IF_AVAILABLE}` | GovDeals description (if present) | `20" wide, 21" deep, 33" tall` |
| `{ESTIMATED_CHAIRS_REMAINING}` | Quantity from GovDeals | "299" |

**⚠️ CRITICAL:** `{LOCATION}` is NEVER hardcoded. It changes for every listing.

### Step 4.3 — Settings (scroll down on Page 1)

| Setting | Value |
|---------|-------|
| Hide from friends | **ON** (toggle) |
| Boost listing after publish | **OFF** (leave default) |

### Step 4.4 — Save Draft
Click **"Save draft"** (top-right) after filling all fields.

### Step 4.5 — User Adds Photos
Tell user to drag cleaned images onto "Add photos" area.

### Step 4.6 — Click Next → Page 2: Delivery Method

| Setting | Value |
|---------|-------|
| Delivery method | Shipping & local pickup |
| Package weight | 12 lb, 0 oz |
| Shipping carrier | USPS Ground Advantage ($17.76) |

Click shipping label section → verify/update in modal → click **Update**.
Click **"Save draft"** again.
Click **Next**.

### Step 4.7 — Page 3: Allow Offers

| Setting | Value |
|---------|-------|
| Let buyers negotiate price | **OFF** |

Click **Next** to publish.

### Confirmation
Facebook redirects to edit page with `listing_id` in URL = listing is live.

---

## PHASE 5: CREATE EBAY LISTING

### Step 5.1 — Navigate to eBay Bulk Sell
```
Claude in Chrome:navigate → url: https://www.ebay.com/bulksell
```

### Step 5.2 — eBay Listing Data Template

| Field | Value |
|-------|-------|
| Format | Buy It Now |
| Title | `{title} - Bulk Deal - Local Pickup` (80 char max) |
| SKU | `BWL-{lot_id}-{city}` |
| Price | `$20.00` per chair (adjust per lot) |
| Quantity | `{quantity}` |
| Condition | Used |
| Category | Business & Industrial > Restaurant & Food Service > Chairs |
| Item Location | `{city}, {state}` |
| Best Offer | Yes |
| Duration | Good 'Til Cancelled |
| Shipping | Local Pickup / Freight |
| Returns | No Returns |
| Handling Time | 3 business days |

### Step 5.3 — eBay Description HTML Template

```html
<h2>{title}</h2>

<p><strong>Bulk Lot — Great Value!</strong></p>

<p>Selling <strong>{quantity} {chair_type}</strong>. Perfect for churches,
banquet halls, conference centers, event venues, nonprofits, schools,
and community centers.</p>

<h3>Details:</h3>
<ul>
  <li><strong>Quantity:</strong> {quantity} chairs available</li>
  <li><strong>Color:</strong> {color}</li>
  <li><strong>Frame:</strong> {frame_material}</li>
  <li><strong>Condition:</strong> Used — good functional condition</li>
  <li><strong>Location:</strong> {LOCATION} (pickup required)</li>
</ul>

<h3>Pricing:</h3>
<ul>
  <li>Individual: ${price_each} each</li>
  <li><strong>Take ALL {quantity} for ${bulk_price}
    (${bulk_price_per_chair}/chair)</strong></li>
</ul>

<p>Message us for freight shipping quotes. Local pickup available.</p>

<p><strong>Black Whole Liquidation — Bulk Chairs at Wholesale Prices</strong></p>
```

---

## FULL AUTOMATION SEQUENCE (Copy-Paste Checklist)

When user provides a GovDeals URL:

```
□ 1. Navigate to GovDeals listing
□ 2. Wait 4 seconds for page load
□ 3. Click carousel right arrow 5+ times
□ 4. Run metadata extraction JS
□ 5. Run image URL extraction JS (both selectors)
□ 6. Generate curl download commands
□ 7. Present curl commands to user
□ 8. Open dewatermark.ai in a tab
□ 9. Tell user to: paste curl → drag to dewatermark → download cleaned
□ 10. Navigate to FB Marketplace create item
□ 11. Fill title, price ($20), category (Dining Furniture Sets)
□ 12. Fill condition (Used - Good)
□ 13. Fill description with DYNAMIC {LOCATION} from GovDeals
□ 14. Scroll down → Hide from friends ON, Boost OFF
□ 15. Click Save draft
□ 16. Tell user to add photos
□ 17. Click Next → Shipping page
□ 18. Verify 12 lb / USPS Ground Advantage $17.76
□ 19. Click Save draft → Next
□ 20. Offers page → negotiation toggle OFF
□ 21. Click Next to publish
□ 22. Confirm listing_id in URL
```

---

## KNOWN LIMITATIONS & WORKAROUNDS

| What | Limitation | Workaround |
|------|-----------|------------|
| Image download | JS downloads unreliable | Use curl commands |
| Watermark removal | OpenCV failed | dewatermark.ai (paid) |
| File upload to FB/dewatermark | Chrome security blocks | User drag-drop |
| FB location | Defaults to user's location | Must verify on delivery page |
| Carousel lazy-loading | Only 2 images load initially | Click next arrow 5+ times |
| Listing page timeout | Chrome tools can freeze | Restart Claude Desktop app |
| GovDeals listing removed | Can't scrape after sold/withdrawn | Scrape while active |

---

## DATA FLOW DIAGRAM

```
GovDeals Listing Page
  ├── <h1> ──────────────────────── → TITLE
  ├── <a> location link ─────────── → LOCATION (⚠️ dynamic!)
  ├── Description text ──────────── → QUANTITY, DIMENSIONS
  ├── .lg-item img / img[src*=assets] → IMAGE URLS
  │
  ▼
Metadata Object:
  {
    title:    "Lot of 299 Tan Metal Folding Chairs",
    location: "Nellis Air Force Base, Nevada, USA",
    city:     "Nellis Air Force Base",
    quantity: "299",
    folder:   "Tan_Metal_Folding_Chairs_Nellis_Air_Force_Base_299",
    urls:     ["https://webassets.lqdt1.com/assets/photos/..."]
  }
  │
  ├──→ curl commands ──→ ~/Desktop/Banquet chiars Pictures/{folder}/
  ├──→ dewatermark.ai ──→ cleaned images in same folder
  ├──→ FB Marketplace description (with {LOCATION} replaced)
  └──→ eBay listing HTML (with {LOCATION} replaced)
```

---

*This blueprint is the single source of truth for the GovDeals automation pipeline.*
*Updated: April 16, 2026 — Session 2*
