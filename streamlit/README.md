# CloseDesk test environment (Streamlit)

A browser version of CloseDesk for trying it out, built for [Streamlit Community Cloud](https://share.streamlit.io). It runs CloseDesk's own code from `src/` over the built-in sample mailbox: the Today list, the mail folders with each email's fraud check and tasks, an attachment's page with where each piece of text was read, Ask (lookup answers), and the fraud check list. You can also upload a file of your own to see how it is read.

**What's different from the laptop app**

- **No local model.** Nothing calls LM Studio. Ask answers by looking things up, as the laptop app does when no model is running; there are no model summaries, vision readings or search by meaning.
- **Sample mailbox only.** No Outlook connection and no drop folder. The board shows the sample's day (Tuesday, September 22, 2026).
- **Nothing is saved.** Each visitor gets their own temporary copy of the sample, which goes when they close the tab or the app restarts. Marking things done, corrections and fraud verdicts aren't offered.
- **Uploads go to Streamlit's servers.** "Try your own file" says so before anything is uploaded. Use sample or made-up files, never real invoices or mail. The limit is 10 MB per file.
- **OCR may be missing.** Scans and pictures are read with RapidOCR when it loads on the server; if it doesn't, the app says "OCR isn't available here" and everything else works.

## Deploy on Streamlit Community Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub (the account must be able to see the repository).
2. Click **Create app** and choose to deploy from a GitHub repository.
3. Repository: `MNabikhel/outlook-email`. Branch: `main`. Main file path: `streamlit/streamlit_app.py`. Pick a URL (`something.streamlit.app`) if you like.
4. Open **Advanced settings** and choose **Python 3.12**. (Changing it after deployment has meant deleting and redeploying the app; check the Streamlit docs.) No secrets are needed.
5. Click **Deploy**. The first build installs `streamlit/requirements.txt` and takes a few minutes; then the app loads the sample mailbox (a second or two per visitor).

Pushing to `main` updates the app automatically.

### Who can see it

- An app deployed from a **private repository** is private by default: only people you invite can open it. An app from a **public repository** is public by default.
- To share it with specific people, open the app's **Settings → Sharing** in your Community Cloud workspace (or the **Share** button on the app) and add their email addresses. They sign in with that email (Google, GitHub or an emailed link) to view it.
- Community Cloud has limits on how many private apps a workspace can have and how sharing works on each plan; check the Streamlit docs ("Share your app") for the current rules.

### Dependencies

Community Cloud looks for a dependency file in the entrypoint's folder first, then at the repository root, so it installs `streamlit/requirements.txt` (and not the laptop app's `requirements.txt`, which stays as it is). That file lists Streamlit (pinned) and CloseDesk's runtime libraries; the app adds `src/` to Python's path itself, so the package isn't installed. FastAPI, Uvicorn and MSAL aren't needed here.

`packages.txt` asks for the system libraries OpenCV needs (`libgl1`, `libglib2.0-0`), which RapidOCR uses. The same file is in the `streamlit/` folder and at the repo root, so Community Cloud finds it whichever place it looks. The laptop app doesn't use either. If OCR still can't load on the server, the app says so and everything else works.

## Run it on your computer

From the repository root, in a Python 3.12 environment:

```bash
pip install -r streamlit/requirements.txt
streamlit run streamlit/streamlit_app.py
```

It opens at [http://localhost:8501](http://localhost:8501). This doesn't touch the laptop app's data folder: everything goes to a temporary folder.

## Tests

`tests/test_streamlit_app.py` runs every page with Streamlit's `AppTest` and checks that none of them fails, that a suspected-fraud email's files never open, and that no network call is made. It is skipped when Streamlit isn't installed (the main test run). To run it:

```bash
pip install -r requirements.txt -r streamlit/requirements.txt
pytest tests/test_streamlit_app.py
```
