# Profile analytics

The profile charts are generated from GitHub's public REST API by [`scripts/update_stats.py`](../scripts/update_stats.py). They use no external statistics-image service, tracking service, browser history, or editor telemetry.

## Scope

- Include public repositories owned by `31916`, including archived repositories.
- Exclude forks, private repositories, and the profile repository `31916/31916` itself. The profile generator must not inflate its own statistics.
- Follow all result pages. An API/authentication error stops the refresh; previously generated files remain untouched. An HTTP 409 is treated as an empty repository only when GitHub explicitly says `Git Repository is empty.`
- The aggregate source is [`assets/stats/data.json`](../assets/stats/data.json). It contains public repository names, default branches, language-byte counts, and aggregate commit counts. It does not contain email addresses, commit messages, raw per-commit timestamps, or personal access tokens.

## Language composition

The language endpoint returns the number of bytes GitHub attributes to each detected language in a repository. The generator sums these bytes across the included repositories and divides each language by the overall byte count. Every detected language is shown, in descending order. Percentages are displayed to one decimal place; a positive value below 0.1% is shown as `<0.1%`.

These are repository code proportions, not a claim of authorship of every line, programming proficiency, time spent, or the complete set of skills. GitHub Linguist's classifications and exclusions apply. HTML, CSS, notebooks, and other detected formats can appear. There is no extra hidden weighting or manual removal of a language to improve the ranking. Rounding can make the displayed percentages differ slightly from 100%.

## Commit activity

The time window is the rolling 90 days ending at the update timestamp in `data.json`. The generator requests all commits attributed to the account from each repository's default branch using GitHub's `author` filter, follows every page, and then checks that:

1. GitHub attributes the commit author account to `31916` (not a bot).
2. The commit has no more than one parent, so merge commits are excluded.
3. Its author timestamp lies within the stated window, including both boundaries.
4. Its SHA has not already been counted from another included repository.

Author timestamps are converted to **Japan Standard Time (UTC+09:00)**. The chart shows the number of matching commits in each hour from 00 to 23. The active-day count is the number of distinct calendar dates in Japan time with at least one matching commit. The 90-day interval can touch 91 calendar dates because it is a rolling timestamp interval. Weekday aggregates in the JSON use Monday through Sunday, in that order.

This does not measure working hours, editor usage, sessions, or time spent on a project. Commit dates can be affected by rebases, imported history, local clock settings, and Git's author/committer date distinction. GitHub's `since` and `until` API filters use last-updated dates; the generator intentionally does not apply them, because doing so could omit commits whose author dates are in the selected window. Unmerged branches, private work, commits in other people's repositories, and commits that GitHub cannot associate with the account are outside the scope. The result is not intended to match the broader GitHub contribution calendar.

## Updating

Run with Python 3.10 or newer:

```text
python scripts/update_stats.py --owner 31916
```

The script uses `GITHUB_TOKEN` when set. Otherwise it uses an already authenticated `gh api` command. It never prints the token. For scheduled execution, a repository-scoped GitHub Actions token is sufficient: these API queries read only public resources, and the workflow commits only the generated files in this repository.

The generator prepares every data file and all six localized SVGs before writing. A failed fetch or render therefore cannot replace the previous charts with a partial data set. If GitHub Actions is delayed or disabled, the visible update date remains the date of the last successful generation; the charts do not pretend to be live.

Use `--now 2026-11-06T00:00:00+09:00` to reproduce the time bounds during development. This does not freeze GitHub's repository content, so language bytes can still change between runs.

Run the focused checks with:

```text
python -m unittest discover -s tests -v
```

## Sources

- [GitHub REST: list repositories for a user](https://docs.github.com/en/rest/repos/repos#list-repositories-for-a-user)
- [GitHub REST: list repository languages](https://docs.github.com/en/rest/repos/repos#list-repository-languages)
- [GitHub REST: list commits](https://docs.github.com/en/rest/commits/commits#list-commits)
- [GitHub Linguist](https://github.com/github-linguist/linguist)
