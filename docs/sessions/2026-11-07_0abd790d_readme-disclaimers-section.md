<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [README Disclaimers section](#readme-disclaimers-section)
  - [Prompt 1](#prompt-1)
  - [Answer 1](#answer-1)
    - [Content](#content)
      - [Format](#format)
      - [Suggested version](#suggested-version)
  - [Prompt 2](#prompt-2)
  - [Answer 2](#answer-2)
  - [Prompt 3](#prompt-3)
  - [Answer 3](#answer-3)
  - [Prompt 4](#prompt-4)
  - [Answer 4](#answer-4)
  - [Prompt 5](#prompt-5)
  - [Answer 5](#answer-5)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

<!-- vale off -->
<!-- rumdl-disable -->

# README Disclaimers section

- Session: `0abd790d-3cc5-4e4a-a68f-fbfb7ecb5916`
- Started: 2026-11-07T15:26:06.700Z
- Folder: `~/workspace/corporate-hotel-booking-software-factory`

---

## Prompt 1

I want to add a "Disclaimers" section at the beginning of the @README.md. I have written down some things, give me your opinion about both the content and the format (should I better use a quoteblock?)

## Answer 1

🍀 Here's my take on the content and the format. I haven't edited the file.

### Content

1. **The third bullet isn't a disclaimer.** "How close can I get to fully unattended delivery" is the experiment's goal. It belongs in the intro, next to "Delegation rises only as fast as the checks underneath it." Right now the same idea shows up twice.
2. **Grammar fixes:**
   - "how much I can approach to" → "how close I can get to"
   - "exactly the same a real-life development" → "exactly the same in real-life development"
   - "trust on" → "trust in"
3. **Tone vs. your own style rules.** "LEARNING" in caps, "many different things" and "high-quality outcomes" are the filler your CLAUDE.md bans. The emojis are a personal-voice choice and fine if you want them, but they clash with the dry tone of the rest of the README.
4. **A disclaimer is missing.** You link a public production URL, so readers will open it. Tell them it's a demo: no real bookings, don't enter personal data, no uptime promise. That's the one a stranger most needs.
5. **Possibly worth saying:** agents wrote most of the code. It's implied, but saying it outright sets expectations for anyone reading the code.

#### Format

⭐ **Use a GitHub alert (`> [!NOTE]`).** It renders as a highlighted box with an icon, reads clearly as "read this first", and is still a plain blockquote on renderers that don't support alerts. doctoc skips it, which is fine because a disclaimer doesn't need a TOC entry. Keep it where it is, after the badges.

❌ **Plain blockquote:** it works, but it looks like a quotation, not a warning.
❌ **`## Disclaimers` heading:** it gives three lines too much weight and adds a TOC entry nobody needs.
❌ **What you have now (italic label + indented list):** it's not a real heading, and the 2-space indent doesn't match the other lists in the file.

#### Suggested version

```markdown
> [!NOTE]
> This repository is a learning experiment. 👨‍🔬🧪
> - I try things here that I would not always do in real-life development.
> - AI agents write most of the code, under the checks described below.
> - The production app is a demo: no real bookings, no uptime promise. Do not enter personal data.
```

Then move your third point into the intro, for example:

> The question: how close can delivery get to fully unattended while the results stay trustworthy? Delegation rises only as fast as the checks underneath it.

I can apply this if you want. Per the repo rules it would need a ticket and a PR.

---

## Prompt 2

I have adapted the disclaimer using your input, what do you think?

## Answer 2

🍀 Much better. The structure works. Four small things left:

1. **The first bullet frames the other three, so it's not a peer of them.** Make it the opening line of the note and keep the three bullets under it. It also still has "LEARNING" in caps and "many different things". Something shorter:

   ```markdown
   > [!NOTE]
   > This repository is a learning experiment. 👨‍🔬🧪
   > - I try things here that I would not always do in real-life development.
   ```

2. **Indentation:** `>   -` has three spaces after the `>`. It renders, but one space (`> - `) matches the other lists in the file.
3. **Stray leading space** before `**The question**` on line 42. Delete it.
4. **"the checks described below" is vague.** The checks are spread over Quick start (`make check`) and Process. A link makes it precise: `under the checks in [Process](#process)`.

One possible issue: README changes on `main` go through a ticket and a squash-merged PR under your repo rules (CHB-116 and CHB-115 were README tickets). If you want, I can open the ticket and PR through `factory/bin/jira` and `factory/bin/as-agent`.

---

## Prompt 3

When I render the README in VS Code, this is how it is shown, is that correct? [Image #1]

## Answer 3

🍀 Your Markdown is correct. The preview is the problem.

- `[!NOTE]` sits alone on its own line, the way GitHub expects. On GitHub that block becomes a blue box with a "Note" title.
- As far as I know, VS Code's built-in preview doesn't support GitHub alerts, so it shows a plain blockquote with `[!NOTE]` as literal text. That's the fallback I mentioned before, and it's what every non-GitHub renderer will show.

To see it the GitHub way locally, install the **Markdown Preview GitHub Styling** extension (`bierner.markdown-preview-github-styles`). It renders alerts. Otherwise, check it on GitHub once the change is pushed.

The first line still reads "LEARNING, learning through experimentation and trying many different things". It's your call, but the shorter "This repository is a learning experiment." would make the note easier to scan.

---

## Prompt 4

OK, I adapted the first sentence and installed the extension you mentioned. This is how it looks like now [Image #2]

## Answer 4

🍀 I was wrong about that extension. It only applies GitHub's look (fonts, colours), so `[!NOTE]` still shows as plain text. Sorry for the wasted install. You can remove it if you don't want GitHub styling in the preview.

To check the real result, I sent the block to GitHub's own Markdown renderer (`gh api markdown`). It comes back as a proper alert: `<div class="markdown-alert markdown-alert-note">` with the info icon, the title "Note", your sentence, and the three bullets. So it will render correctly on GitHub.

The content is fine. One nit is left: "LEARNING" is still in caps. If that's deliberate emphasis, keep it.

---

## Prompt 5

Yes, that's deliberate emphasis. What about using [!Disclaimer] instead of [!NOTE]?

## Answer 5

🍀 That won't work. GitHub only knows five alert types: `NOTE`, `TIP`, `IMPORTANT`, `WARNING` and `CAUTION`. I ran `[!DISCLAIMER]` through GitHub's renderer and got a plain blockquote with `[!DISCLAIMER]` as literal text, the same thing VS Code showed you.

⭐ **Keep `[!NOTE]`.** It's blue, calm and informational, which matches what the block says.

❌ `[!IMPORTANT]` (purple): it fits only if you want readers to stop before they scroll. That's too strong for this content.
❌ `[!WARNING]` / `[!CAUTION]` (yellow / red): those suggest danger. The "no personal data" line doesn't justify that tone.
❌ A custom label like `> **Disclaimer**`: it's possible, but you lose the box and the icon.

If you want the word "Disclaimer" visible, put it inside the note as its first line: `> **Disclaimer:** This repository is a LEARNING experiment 👨‍🔬🧪`. The box title still reads "Note".
