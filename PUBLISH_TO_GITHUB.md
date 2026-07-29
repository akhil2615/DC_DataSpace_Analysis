# Publish To GitHub

1. Create a new empty repo in GitHub (no README).
2. Run:

```bash
cd C:\Users\cakhil\Developer\dc-data-space-analysis
git init
git add .
git commit --trailer "Co-authored-by: Cursor <cursoragent@cursor.com>" -m "Initial DC data space analysis pipeline"
git branch -M main
git remote add origin <YOUR_NEW_REPO_URL>
git push -u origin main
```
