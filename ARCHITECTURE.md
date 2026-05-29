# Listing Automation — Architecture (as of 2026-04-21)

This document is a **living map** of the system as it exists today, derived from
`CLAUDE.md` + the current repo layout. It is meant to be edited directly:

- Every diagram is Mermaid → edit the text, the picture follows.
- Leave comments in `<!-- COMMENT: ... -->` blocks next to the thing you're
  critiquing. GitHub/VS Code both render these as hidden in the preview but
  visible in raw edit mode.
- The "Rebuild notes" section at the bottom is where to stage proposed
  changes before they touch code.

Legend used throughout:

- **Solid arrow** = runtime call / HTTP / subprocess spawn
- **Dashed arrow** = read/write to persistent state
- **Blue fill** = our code
- **Yellow fill** = external service
- **Gray fill** = local persistent storage

---

## 1. System context — who talks to whom

```mermaid
flowchart LR
    user([👤 Operator])
    customer([👤 Public site visitor])

    subgraph local["Local machine (macOS)"]
        cli["run.py CLI"]
        web["FastAPI app<br/>(automation.web)"]
        scraper["auction_extractors<br/>scraper subprocess"]
        chrome["Chromium<br/>(Playwright persistent ctx)"]
    end

    subgraph external["External services"]
        gd["GovDeals.com"]
        ps["PublicSurplus.com"]
        fb["Facebook Marketplace"]
        ebay["eBay Seller Hub"]
        dwm["dewatermark.ai API"]
        gemini["Gemini API"]
        openai["OpenAI API"]
        ollama["Ollama (local/cloud)"]
    end

    user -->|paste URL, confirm price| web
    user -->|python run.py URL| cli
    customer -->|browse / inquire| web

    web -->|spawn| cli
    web -->|spawn| scraper
    cli -->|Playwright| chrome
    scraper -->|Playwright| chrome

    chrome --> gd
    chrome --> fb
    chrome --> ebay
    scraper --> gd
    scraper --> ps

    cli --> dwm
    cli --> gemini
    cli --> openai
    scraper --> ollama

    classDef ours fill:#c7d9ff,stroke:#2b4b99,color:#111
    classDef ext fill:#ffe9a8,stroke:#9a7a12,color:#111
    class cli,web,scraper,chrome ours
    class gd,ps,fb,ebay,dwm,gemini,openai,ollama ext
```

<!-- COMMENT:
     - Anything missing from this view?
     - Should the scraper really share a browser profile with the pipeline?
-->

---

## 2. Module/package layout — what's in the repo

```mermaid
flowchart TB
    subgraph entry["Entry points"]
        runpy["run.py<br/>CLI + phase orchestrator"]
        webmod["automation/web/<br/>(__main__.py, app.py)"]
    end

    subgraph core["automation/ — pipeline core"]
        config["config.py<br/>paths, env, defaults"]
        browser["browser.py<br/>Playwright persistent ctx<br/>(auto-clears stale lock)"]
        progress["progress.py<br/>&lt;&lt;&lt;EVENT&gt;&gt;&gt; emitter"]
        inventory["inventory.py<br/>SQLite ledger<br/>inventory + inquiries tables"]
        templates["templates.py<br/>FB + eBay description HTML"]

        subgraph phases["Pipeline phases"]
            govdeals["govdeals.py<br/>scrape (Phase 1)"]
            llm_pkg["llm/*<br/>extractors"]
            downloader["downloader.py<br/>httpx + CDN headers"]
            dewatermark["dewatermark.py<br/>+ dewatermark_cache.py"]
            quality["quality.py<br/>byte-identity check"]
            facebook["facebook.py<br/>FB draft"]
            ebay["ebay.py<br/>eBay draft"]
        end
    end

    subgraph llm["automation/llm/ — extractors"]
        base["base.py (protocol)"]
        gemini_ext["gemini.py (primary)"]
        openai_ext["openai.py (A/B secondary)"]
        claude_ext["claude_code.py<br/>TTY-only"]
        ollama_ext["ollama_local.py"]
        dom["dom_fallback.py<br/>pure heuristic"]
        picker["__init__.default_extractors()<br/>env-aware picker"]
    end

    subgraph web_pkg["automation/web/ — FastAPI app"]
        app["app.py<br/>routes + run queue"]
        templates_dir["templates/<br/>landing, index, listings, ..."]
        static["static/<br/>app.css (admin)<br/>public.css (public)<br/>app.js / public.js"]
    end

    subgraph sibling["auction_extractors/ — sibling package"]
        top["top_chairs.py<br/>get_top_chairs() (read-only API)"]
        gd_scrape["govdeals_chairs_extraction.py"]
        ps_scrape["public_surplus_automation.py"]
        listings_db["state/listings.db<br/>(upstream cache)"]
    end

    runpy --> phases
    runpy --> inventory
    runpy --> progress
    runpy --> config
    phases --> browser
    phases --> progress

    llm_pkg --- llm
    picker --> gemini_ext
    picker --> openai_ext
    picker --> claude_ext
    picker --> ollama_ext
    picker --> dom

    webmod --> app
    app --> inventory
    app -->|spawn subprocess| runpy
    app -->|spawn subprocess| gd_scrape
    app -->|spawn subprocess| ps_scrape
    app -->|reads| top
    top -.->|SELECT| listings_db
    gd_scrape -.->|INSERT| listings_db
    ps_scrape -.->|INSERT| listings_db

    classDef ours fill:#c7d9ff,stroke:#2b4b99,color:#111
    class runpy,webmod,config,browser,progress,inventory,templates,govdeals,llm_pkg,downloader,dewatermark,quality,facebook,ebay,base,gemini_ext,openai_ext,claude_ext,ollama_ext,dom,picker,app,templates_dir,static,top,gd_scrape,ps_scrape ours
```

<!-- COMMENT:
     - Is the top-level split (entry / core / llm / web / auction_extractors)
       the right cleave? If rebuilding from scratch, what would you pull out
       into its own package vs. inline?
     - run.py is doing a LOT (CLI parsing + orchestration + ledger consults
       + price-confirm UX). Candidate for splitting.
-->

---

## 3. Pipeline flow — one URL → two drafts

This is the happy path through `run.py`. Each phase emits `<<<EVENT>>>` lines
consumed by the dashboard.

```mermaid
sequenceDiagram
    autonumber
    actor U as Operator
    participant W as Web (app.py)
    participant R as run.py
    participant GD as govdeals.py
    participant LLM as llm/ picker
    participant DL as downloader.py
    participant DW as dewatermark.py
    participant Q as quality.py
    participant FB as facebook.py
    participant EB as ebay.py
    participant INV as inventory.py
    participant API as dewatermark.ai
    participant B as Chromium

    U->>W: POST /api/runs/start {url}
    W->>R: spawn subprocess (url)

    R->>INV: get(lot_id)
    Note over R,INV: dedup check — skip phases if URLs already present

    R->>GD: scrape(url)
    GD->>B: navigate + extract images
    GD-->>R: meta (title, qty?, images, screenshots in scratch/)

    R->>LLM: extract(html, screenshots)
    LLM-->>R: ListingData (primary + secondary A/B)
    R->>GD: finalize_folder(meta, qty)
    Note over R,GD: mkdir real folder, move screenshots in

    R->>U: emit "price" event (suggested N)
    U->>W: POST /api/runs/stdin {line: "12"}
    W->>R: stdin → confirmed price

    R->>DL: download all images (with Referer + UA)
    DL-->>R: files in <folder>/

    loop for each image not in cache
        R->>DW: clean(image)
        DW->>API: POST /erase_watermark
        API-->>DW: cleaned bytes
        DW->>Q: watermark_likely_present(cleaned, original)
        Q-->>DW: OK | reject (byte-identical)
        DW-->>R: written to folder, original moved to _originals/
    end

    alt facebook_url missing OR --force-republish
        R->>FB: publish(folder, meta, price)
        FB->>B: fill FB draft
        FB-->>R: fb_url
        R->>INV: set_platform_url(lot, "facebook", fb_url)
    else already listed
        R-->>R: emit "skipped_duplicate"
    end

    alt ebay_url missing OR --force-republish
        R->>EB: publish(folder, meta, price)
        EB->>B: fill eBay draft
        EB-->>R: ebay_url
        R->>INV: set_platform_url(lot, "ebay", ebay_url)
    else already listed
        R-->>R: emit "skipped_duplicate"
    end

    R->>INV: upsert_from_run(meta, price)
    R-->>W: exit 0
```

<!-- COMMENT:
     - Price confirmation is synchronous & blocking. Worth keeping?
     - The dedup check happens per-phase — would a single up-front "what needs
       doing" resolver be cleaner?
     - No retry logic anywhere. FB/eBay failures currently just degrade.
-->

---

## 4. Persistence layout — where state lives

```mermaid
flowchart LR
    subgraph app_state["~/.listing_automation/"]
        chrome_profile["chrome_profile/<br/>Playwright persistent profile<br/>(logged into FB + eBay)"]
        inv_db[("inventory.db<br/>SQLite<br/>• inventory<br/>• inquiries")]
        api_cache["api_cache/&lt;sha&gt;.bin<br/>global dewatermark cache"]
        logs["logs/llm_compare_*.json<br/>A/B + dewatermark_usage.jsonl"]
        scratch["scratch/&lt;lot&gt;_&lt;ts&gt;/<br/>pre-LLM screenshots"]
    end

    subgraph desktop_state["~/Desktop/Banquet chiars Pictures/"]
        folder["&lt;lot&gt;/<br/>cleaned images"]
        originals["&lt;lot&gt;/_originals/<br/>raw downloads"]
        screens["&lt;lot&gt;/_screenshots/<br/>LLM evidence"]
        sidecar["&lt;lot&gt;/.dewatermark_state.json<br/>per-folder sha→status"]
    end

    subgraph upstream["auction_extractors/state/"]
        listings_db[("listings.db<br/>SQLite<br/>upstream scrape cache<br/>READ-ONLY from main app")]
    end

    subgraph env_files[".env files"]
        root_env[".env<br/>DEWATERMARK_API_KEY<br/>GEMINI_API_KEY, ..."]
        ae_env["auction_extractors/.env<br/>OLLAMA_URL, OLLAMA_MODEL, ..."]
    end

    classDef storage fill:#dcdcdc,stroke:#555,color:#111
    class chrome_profile,inv_db,api_cache,logs,scratch,folder,originals,screens,sidecar,listings_db,root_env,ae_env storage
```

Two tables inside `inventory.db`:

```mermaid
erDiagram
    INVENTORY ||--o{ INQUIRIES : "lot_id (nullable FK)"
    INVENTORY {
        string lot_id PK
        string folder_name
        string folder_path
        string sku
        string title
        string city
        string chair_type
        int quantity_original
        int quantity_remaining
        float price_per_chair
        string hero_image
        string status "draft|listed|hidden|sold_out"
        string facebook_url
        datetime facebook_published_at
        string ebay_url
        datetime ebay_published_at
        datetime parsed_at
        datetime updated_at
    }
    INQUIRIES {
        int id PK
        string kind "buy|sell"
        string lot_id FK "nullable"
        string name
        string email
        string phone
        string message
        string status "new|contacted|closed"
        datetime created_at
    }
```

<!-- COMMENT:
     - 4 different places state lives (~/.listing_automation, ~/Desktop/...,
       auction_extractors/state, .env files). Consolidate?
     - listings.db is explicitly read-only from the main app. If rebuilding,
       does that separation still make sense, or should there be one DB?
-->

---

## 5. Web surfaces — one FastAPI process, two audiences

```mermaid
flowchart TB
    subgraph fastapi["FastAPI process (python -m automation.web, :8765)"]
        direction TB

        subgraph public["Public site (brutalist-industrial theme)"]
            p_root["GET /<br/>landing.html<br/>live stats + featured strip"]
            p_list["GET /listings<br/>card grid + client-side filters"]
            p_detail["GET /listings/{lot_id}<br/>gallery + spec + inquiry form"]
            p_sell["GET /sell<br/>seller intake form"]
            p_contact["POST /contact<br/>→ inquiries table"]
        end

        subgraph admin["Admin dashboard (/admin, dark terminal theme)"]
            a_root["GET /admin<br/>index.html (tabs)"]
            t1["01 Launcher<br/>URL input + UP NEXT queue"]
            t2["02 Drafts<br/>live pipeline log"]
            t3["03 A/B<br/>LLM compare + dewatermark usage (TODO)"]
            t4["04 Auctions<br/>auction_extractors cards<br/>+ scrape button + staleness banner"]
            t5["05 Inventory<br/>editable table"]
            t6["06 Inquiries<br/>chronological cards"]
        end

        subgraph api["Admin JSON APIs"]
            api_inv["/api/inventory<br/>/api/inventory-stats<br/>/api/inventory/backfill<br/>/api/inventory/{lot_id}/platform"]
            api_inq["/api/inquiries (list/patch/delete)"]
            api_runs["/api/runs/start (legacy)<br/>/api/runs/queue<br/>/api/runs/queue/clear<br/>/api/runs/stdin"]
            api_auc["/api/auctions (cached 10min)<br/>/api/auctions/refresh<br/>/api/auctions/cache-stats<br/>/api/auctions/scrape"]
            api_media["/image/*  /screenshot/*  /static/*"]
        end
    end

    p_root -.reads.-> inv_db2[("inventory.db")]
    p_list -.reads.-> inv_db2
    p_detail -.reads.-> inv_db2
    p_contact -.writes.-> inv_db2

    t5 <-->|CRUD| api_inv
    t6 <-->|CRUD| api_inq
    t1 -->|POST| api_runs
    t4 <-->|GET/POST| api_auc

    api_runs -->|spawn serially| runpy["run.py subprocess"]
    api_auc -->|spawn| gd_scrape2["govdeals scraper subprocess"]
    api_auc -->|spawn| ps_scrape2["public_surplus scraper subprocess"]

    api_inv -.->|SQL| inv_db2
    api_inq -.->|SQL| inv_db2
    api_auc -.->|read-only SELECT| listings_db2[("listings.db")]

    classDef ours fill:#c7d9ff,stroke:#2b4b99,color:#111
    classDef storage fill:#dcdcdc,stroke:#555,color:#111
    class p_root,p_list,p_detail,p_sell,p_contact,a_root,t1,t2,t3,t4,t5,t6,api_inv,api_inq,api_runs,api_auc,api_media,runpy,gd_scrape2,ps_scrape2 ours
    class inv_db2,listings_db2 storage
```

<!-- COMMENT:
     - Public + admin in one process — keep for simplicity, or split for
       deploy-ability?
     - The run queue lives in process memory (state.pending). Restart =
       lose the queue. Worth persisting?
-->

---

## 6. Observability / events

```
run.py emits:      <<<EVENT>>>{"phase": "...", "status": "...", ...}\n
dashboard parses:  automation/web/app.py tails subprocess stdout line-by-line
storage:           ~/.listing_automation/logs/
                     - llm_compare_*.json   (per-run A/B log)
                     - dewatermark_usage.jsonl  (API call log)
```

<!-- COMMENT:
     - No structured log aggregation. Fine for single-user. Would need work
       before multi-tenant.
-->

---

## 7. Rebuild notes — staging ground for v2

This section is where to draft proposed changes *before* they become code.
Keep them short; each one should map to an eventual commit or PR.

### 7.1 Known rough edges (from CLAUDE.md "TODOs" + Gotchas)

| # | Pain | Where it lives today |
|---|------|----------------------|
| 1 | eBay flow doesn't land on a real draft URL | `automation/ebay.py` selectors |
| 2 | FB + eBay selectors are best-effort, print `[fallback]` warnings | `automation/facebook.py`, `automation/ebay.py` |
| 3 | GovDeals quantity regex `\((\d{1,5})\)` is brittle | `automation/govdeals.py` `EXTRACT_JS` |
| 4 | No cost-tracking tile on dashboard | `automation/web/templates/index.html` 03 A/B tab |
| 5 | Price confirm is synchronous, blocks the pipeline | `run.py` price-confirm loop |
| 6 | Run queue is in-memory only | `automation/web/app.py` `state.pending` |
| 7 | Desktop location blocks launchd/cron for auto-scrape | moving the repo fixes it |
| 8 | `run.py` carries too many responsibilities | CLI + orchestrator + UX + ledger |

### 7.2 Architectural bets to decide on

<!-- COMMENT: Edit these freely. One-liners, not essays. -->

- [ ] **Single DB vs. two DBs.** Merge `listings.db` into `inventory.db`
      with a clear "upstream_cache" table, or keep the read-only wall?
- [ ] **Split `run.py`.** Candidates: `orchestrator.py` (phase runner),
      `cli.py` (argparse), `price_prompt.py` (TTY/dashboard bridge).
- [ ] **Phase abstraction.** Today each phase is a bespoke module.
      A `Phase` protocol (`name`, `should_run(ctx)`, `run(ctx)`, `record(ctx)`)
      would make dedup + retry + progress uniform.
- [ ] **Retry/resume.** FB or eBay fails → currently no way to resume
      just that phase. Ledger has enough info to make this possible.
- [ ] **Config surface.** `.env` + `automation/config.py` + two separate
      `.env` files (root + `auction_extractors/`) is fragmented. One
      settings model (pydantic-settings) that loads both?
- [ ] **Queue persistence.** Write `state.pending` to disk so dashboard
      restart doesn't lose the backlog.
- [ ] **Public site split.** Does the customer-facing site want to be its
      own process (or even static export) so admin-only deploys don't
      restart the public surface?
- [ ] **Platform abstraction.** Shared interface for FB/eBay (and future
      Craigslist/OfferUp) — `Publisher.publish(folder, meta) -> url`.

### 7.3 Scope of "v2"

<!-- COMMENT: Cut or expand. The point of this file is to argue about scope. -->

- **In scope:** anything that makes the pipeline more resumable, more
  observable, or cheaper to extend with new platforms.
- **Out of scope (for now):** multi-user, auth, cloud deploy, non-macOS.

---

## 8. How to update this file

- Edit Mermaid text directly. VS Code's "Markdown Preview Mermaid Support"
  extension renders live.
- Keep the five views (context / modules / flow / storage / web) as the
  top-level structure even if individual boxes change.
- When a rebuild decision from §7.2 actually lands, move it to a brief
  "Done" line at the top of §7 with a date, and update the diagrams.
- If a diagram gets too busy to edit comfortably, split it rather than
  shrinking nodes.
