# Linking the downloader into Manuscript Workbench

Your app already downloads OA PDFs and re-indexes them. This adds two things to
the **Get Papers** tab:

1. **Resilient OA downloading** — retries dead OpenAlex links via Unpaywall /
   arXiv / PubMed Central and verifies real PDFs.
2. **Authenticated fetch of the 🔒 paywalled items** — through a UHasselt browser
   session you establish yourself (no stored password), instead of only exporting
   a DOI list.

Both write PDFs under your existing `config.PDF_DIR`, so your current
**"Re-index new papers"** button ingests them with no change.

## 1. Files to copy into `C:\Users\krishn28\Paper rag`

- The whole `literature/` package folder (from this repo).
- `integrations/workbench_downloader.py` → put it next to `workbench.py`.

## 2. One-time install (for the paywalled path only)

```powershell
pip install playwright
python -m playwright install chromium
```

The OA path needs nothing new — it uses `requests`, which you already have.

## 3. Edits to `workbench.py`

### a) Import the bridge in the Get Papers tab

Near the top of `with tab_get:` (where `import get_papers as gp` is), add:

```python
    import workbench_downloader as wdl
```

### b) A contact email input (Unpaywall etiquette)

Unpaywall/Crossref ask for a contact email. Add one input inside the tab, e.g.
just under the intro `st.markdown(...)` block:

```python
    dl_email = st.text_input(
        "Contact email for open-access lookups",
        value="anurag.krishna@uhasselt.be",
        help="Sent to Unpaywall/Crossref as an identifier — good etiquette, "
             "and keeps the lookups from being throttled.")
```

### c) Upgrade the "Download all OA PDFs" button (optional but recommended)

Replace the body of the `with d1:` block so it uses the resilient resolver:

```python
        with d1:
            if st.button("⬇️ Download all OA PDFs",
                         use_container_width=True, disabled=n_oa == 0):
                bar = st.progress(0.0)
                def _cb(i, total, title):
                    bar.progress(i / total, text=f"{i}/{total}: {title[:60]}")
                n_ok, n_fail, msgs = wdl.download_oa_resolved(
                    results, config.PDF_DIR, dl_email.strip(), _cb)
                bar.empty()
                st.success(f"Downloaded {n_ok} PDF(s) into "
                           f"'{config.PDF_DIR}/{wdl.OA_SUBDIR}'. "
                           f"{n_fail} had no reachable OA copy.")
                with st.expander("Download log"):
                    st.text("\n".join(msgs))
```

### d) Add the paywalled / institutional section

Add this new block right after the `d1, d2, d3` columns (before the `st.markdown("---")`
that starts the Zotero section):

```python
    if results and any(not w["pdf_url"] for w in results):
        n_locked = sum(1 for w in results if not w["pdf_url"])
        with st.expander(f"🔒 Fetch {n_locked} paywalled item(s) via UHasselt access"):
            st.caption(
                "Uses a browser session you log in to yourself (SSO + MFA). "
                "The tool never sees your password. For papers you have "
                "institutional access to; conservatively rate-limited and "
                "intended for small batches, not bulk collection.")
            p1, p2 = st.columns(2)
            with p1:
                if st.button("① Set up / refresh UHasselt login",
                             use_container_width=True):
                    wdl.setup_login(dl_email.strip())
                    st.success("Session saved. You can download below.")
            with p2:
                if st.button("② Download paywalled PDFs",
                             use_container_width=True):
                    bar = st.progress(0.0)
                    def _cbp(i, total, title):
                        bar.progress(i / total, text=f"{i}/{total}: {title[:60]}")
                    n_ok, n_fail, msgs = wdl.download_paywalled_via_session(
                        results, config.PDF_DIR, dl_email.strip(), _cbp)
                    bar.empty()
                    st.success(f"Fetched {n_ok} PDF(s) into "
                               f"'{config.PDF_DIR}/{wdl.AUTH_SUBDIR}'. "
                               f"{n_fail} not reachable (likely no access).")
                    with st.expander("Fetch log"):
                        st.text("\n".join(msgs))
```

Note: `setup_login` opens a **visible** browser window, so run the app locally
(as you do) rather than headless. After you log in once, the session persists in
`~/.my-literature/browser-profile` and step ② reuses it.

### e) Re-index

No change — your existing **"🔄 Re-index new papers"** button runs `ingest.py`
over `config.PDF_DIR`, which now includes the `openalex_resolved/` and
`institutional/` subfolders.

## Why keep this as an explicit, opt-in section

Your app's current design is deliberately careful — OA auto-downloads, paywalled
items go to a DOI list. The institutional fetch is legitimate for papers you can
already read, but it's the kind of thing that should be a conscious click, not a
silent default. That's why it's behind an expander with its own login step and a
conservative rate limit, rather than folded into the main "Download all" button.
For a large systematic corpus, still prefer the publisher TDM API route your
legality note already recommends (see `docs/tdm-apis.md`).
