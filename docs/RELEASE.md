# Releasing rh-mcp to GitHub

One-time setup to publish your fork so friends can install it.

## 1. Replace placeholders

Five files reference `YOUR_USERNAME`. Replace with your GitHub handle:

- `README.md`
- `pyproject.toml`
- `docs/INSTALL.md`
- `claude_mcp_config.example.json`
- (rh_mcp.egg-info regenerates on next `pip install -e .` — ignore)

Quick replace from PowerShell:

```powershell
$gh = "yourname"  # <-- set your GitHub username
Get-ChildItem -Recurse -Include *.md,*.toml,*.json -Exclude rh_config.json,*.egg-info |
  ForEach-Object { (Get-Content $_.FullName) -replace 'YOUR_USERNAME', $gh | Set-Content $_.FullName }
```

Or from bash:

```bash
GH=yourname
grep -rl YOUR_USERNAME . --exclude-dir=rh_mcp.egg-info | xargs sed -i "s/YOUR_USERNAME/$GH/g"
```

## 2. Verify nothing sensitive is staged

```bash
git init
git add -A
git status
```

`rh_config.json` and `robinhood.pickle` should be ignored (via .gitignore). If you see them in `git status`, **stop** and fix `.gitignore` before committing.

## 3. Initial commit

```bash
git commit -m "Initial release: rh-mcp v0.1.0 with 53 tools"
```

## 4. Create the GitHub repo + push

Option A — with `gh` CLI installed:

```bash
gh repo create rh-mcp --public --source=. --remote=origin --push
```

Option B — manual:

1. Create an empty repo at https://github.com/new (name: `rh-mcp`, no README/license/gitignore — we have those already)
2. ```bash
   git remote add origin https://github.com/yourname/rh-mcp.git
   git branch -M main
   git push -u origin main
   ```

## 5. Verify the repo

- README renders cleanly on the repo home
- `docs/TOOLS.md` is visible
- `LICENSE` shows "MIT" badge if you enable GitHub's license detection
- No `rh_config.json` in the file tree (cross-check)

## 6. Share with friends

Send them: `https://github.com/yourname/rh-mcp`

They follow `docs/INSTALL.md`:
1. `git clone` the repo
2. `pip install -e .`
3. `cp rh_config.example.json rh_config.json` + fill in their own credentials
4. Add the MCP config snippet to their `.claude/settings.json`
5. Restart Claude Code

## Future updates

Bump version in `pyproject.toml` (`version = "0.1.1"` etc.), update `README.md` / `docs/STATUS.md` with what changed, commit + push.

For a tagged release:

```bash
git tag -a v0.1.1 -m "Release v0.1.1"
git push origin v0.1.1
```

## Optional: publish to PyPI

If you want `pip install rh-mcp` to work without git clone:

```bash
pip install build twine
python -m build
python -m twine upload dist/*
```

You need a PyPI account first. Free, but you'll need to verify your email and add an API token to your environment.
