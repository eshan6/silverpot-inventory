# Amazon Ads support case: POST /history rejects every eventTypes filter

This is written out ready to paste into an Amazon Ads API support case. It is
here rather than in a chat log because the evidence took six CI round trips to
collect and should not have to be collected again.

File it at <https://advertising.amazon.com/API/docs/en-us/support> (Developer
support, "API issue"), or through the Contact Us link in the Ads console.

Fill in the two bracketed values before sending. Neither is a secret, but
neither is in this repository: the client id is on the My Apps page of the Ads
developer console, and the profile id is the `ADS_PROFILE_ID` Actions secret.
**Do not paste the client secret or the refresh token.** Support does not need
them and a case thread is not a safe place for either.

---

**Subject:** POST /history rejects every non-empty `eventTypes` value with
`Key in HistoryEventTypes: 0 is not supported`

**Client ID:** [amzn1.application-oa2-client....]
**Profile ID:** [ADS_PROFILE_ID]
**Marketplace:** US (ATVPDKIKX0DER)
**Scope on the app:** `advertising::campaign_management`
**Date observed:** 2026-10-20

### What I am trying to do

Read campaign change history for my own advertiser account, so that campaign
and bid edits can be correlated against daily spend and sales in an internal
reporting database. Read-only.

### The request

```
POST https://advertising-api.amazon.com/history
Amazon-Advertising-API-ClientId: <client id>
Amazon-Advertising-API-Scope: <profile id>
Authorization: Bearer <LWA access token>
Content-Type: application/json

{
  "fromDate": 1784886000000,
  "toDate": 1792489200000,
  "eventTypes": ["CAMPAIGN"],
  "pageSize": 50,
  "pageOffset": 0
}
```

### The response

```
HTTP 400
Key in HistoryEventTypes: 0 is not supported. Needs to be one of
CAMPAIGN,AD_GROUP,AD,KEYWORD,PRODUCT_TARGETING,NEGATIVE_KEYWORD,
NEGATIVE_PRODUCT_TARGETING,THEME
```

`CAMPAIGN` is the first value the error message itself lists as acceptable.

### What works

The same request with `"eventTypes": []` returns **HTTP 200** and a
well-formed empty page:

```json
{"events": [], "pageSize": 50, "pageOffset": 0,
 "maxPageNumber": 0, "totalRecords": 0}
```

So the endpoint is reachable, the token is accepted, the auth headers are
right, and `fromDate` / `toDate` are validated and accepted (an out-of-range
`fromDate` is refused with a different and correct message:
`fromDate is more than 90 days ago, it should be greater than 1784713305808`).
Only the `eventTypes` filter is refused.

### What I have already ruled out

Eleven request variants across three independent dimensions, all returning
that identical message:

1. **Value encoding** - array of strings `["CAMPAIGN"]`; all eight values;
   lowercase values; a map of name to true `{"CAMPAIGN": true}`; a map of
   index to name `{"0": "CAMPAIGN"}`; a comma-separated string
   `"CAMPAIGN,AD_GROUP"`; an array of objects `[{"eventType": "CAMPAIGN"}]`;
   a bare string `"CAMPAIGN"`.
2. **Content type** - `application/json`,
   `application/vnd.history.v1+json`, `application/vnd.historyevents.v1+json`.
3. **Field naming** - `startDateTime` / `endDateTime` / `recordTypeFilter`
   were refused with a clear and different message naming `eventTypes`,
   `fromDate` and `toDate` as the required fields, so the field names above
   are the ones the endpoint asks for.

The `0` in the message does not vary with the request. It is identical for an
array, for a map keyed by name, and for a bare string - so it does not appear
to be an index into anything I sent.

I also checked that this is not a missing entitlement or an unreleased
feature:

- The merged OpenAPI specification
  (`AmazonAdsAPIALLMerged_prod_3p_ALL.json`) covers the `/adsApi/v1/...`
  surface only. It contains no `/history` path and no `HistoryEventTypes`
  schema, so I could not read the expected request schema from it.
- Change History is not listed on the API betas page, so there is no opt-in
  I am missing.
- The Amazon Ads MCP server (`advertising-ai.amazon.com/mcp`) exposes 110
  tools to this same account, and none of them reads change history. Its own
  tool search, asked for "change history audit log of past edits", returns
  the campaign *mutation* tools and nothing that reads past changes.

### What I need

One of:

1. The exact JSON shape `eventTypes` expects, such that a request for
   `CAMPAIGN` is accepted; or
2. Confirmation that this endpoint is not available to my account or
   application, and what entitlement or role would make it available; or
3. The documented endpoint I should be using instead to read campaign change
   history programmatically.

Happy to supply request ids or run any request shape you want to see the
response to.
