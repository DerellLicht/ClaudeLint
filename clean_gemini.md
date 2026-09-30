Steps for cleaning Gemini posts in markdown files, saved from `Save as Markdown` Firefox extension

1\. remove title block:
Block is at top of file, begins and ends with `---`
```
---
title: "Orb Weaver Web Removal Behavior"
source: "Google Search"
date_saved: "2026-09-30T17:13:05.149Z"
word_count: "1168"
reading_time: "6 min"
---
```

2\. Remove `[Accessibility help]` if present, before first post

3\. Remove the line beginning with `## AI Mode Conversation: `

4\. Remove images; they either:  
begin with `[![WNBA Playoffs 2026](data:image` and end with `)`  
or begin with `![](data:image` and end with `)`

5\. remove ad blocks  
they begin with:
```
Copied to clipboardFailed to copy to clipboard. Try again later.

## Shared
```
and end with:  
```
Show all
```

6\. clean up *my* posts  
Each time I leave a post, it is stored in the file like this:  
`## You said: [my entire post]`

Followed by  
`[my entire post]`  
(without the square brackets)

I would like the entire `## You said: ` block replaced with `## Me  `

7\. clean up Gemini's posts  
Each of Gemini's posts is preceded by:  
`### AI Mode reply for [my entire previous post`

I would like that entire block replace with `## Gemini  `
