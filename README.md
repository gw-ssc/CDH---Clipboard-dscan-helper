# Clipboard dscan Helper

A lightweight Python desktop tool for handling EVE Online d-scan and local scan clipboard data.

The application watches your clipboard, detects whether copied content is:

- EVE d-scan output
- EVE local scan output
- URLs
- JSON
- Code snippets
- General text

For EVE d-scan workflows, it can automatically:

1. Detect and submit a d-scan to dscan.info
2. Extract the returned token
3. Build the resulting URL
4. Wait for the next local scan clipboard copy
5. Submit the local scan to the stored d-scan page
6. Copy the final result URL back into the clipboard

---

# Features

- Clipboard monitoring
- Automatic d-scan detection
- Automatic local scan detection
- dscan.info HTML form submission
- Automatic token parsing (`OK;<token>`)
- Automatic URL generation
- Local overview/statistics
- Tkinter GUI
- Result history
- Config persistence
- Optional browser auto-open
- Optional automatic local submission workflow

---
