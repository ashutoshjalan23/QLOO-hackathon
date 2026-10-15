# Project history and scope

RainCheck builds on a group booking agent from September 2026. The root commit
message credits the earlier Shum-AI work by Aikagra Gupta, Prakhar Khandelwal,
and Akshay Thanipet Padmanabhan. The MIT license and that attribution remain.

The inherited base includes Telegram coordination, model/fallback extraction,
OSM/Exa discovery, voice workflows, and booking state. Later commits add private
requirements, negotiation boundaries, Vapi integration, result/calendar handling,
and the RainCheck name. The final two original commits add Qloo documentation
and the adapter. The current maintenance update corrects the adapter's contracts,
authentication, response handling, tests, and documentation.

At the maintainer's request on October 16, 2026, the original 44 commits' author
and committer timestamps were redistributed over October 1–16, preserving order,
messages, authors, and file trees. Those timestamps do not establish when the
inherited September work was originally created. Commit IDs changed; the root
is now `0f31315` (formerly `1d5c730`). One original commit signature could not be
retained after the timestamp change.

[`product-direction.md`](product-direction.md) is a historical review prepared
on September 19 for an earlier event. Its dates, prior repository names, and
event discussion are preserved as historical context, not current setup or
current submission instructions. Use the root README and README_QLOO for the
current repository's supported behavior and installation steps.
