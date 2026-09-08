import { Freshness } from "../components/Freshness";
import type { Marketplace } from "../lib/types";

/**
 * Impressions, clicks, CPC, attributed sales and search terms.
 *
 * Not built yet, and blocked on something outside this repository: the Amazon
 * Advertising API is a separate registration from SP-API with its own
 * approval. The schema is ready - ads_daily and ads_search_terms carry all
 * three ad programs - so this page is the only thing waiting.
 *
 * Deliberately not a mock. Placeholder numbers on a page that looks finished
 * are how people end up making spend decisions on invented data.
 */
export default function Ads({ marketplace }: { marketplace: Marketplace }) {
  return (
    <>
      <Freshness marketplace={marketplace} />
      <div className="empty">
        <strong>Ads data is not connected yet.</strong>
        Impressions, clicks, CPC, attributed sales and search terms come from
        the Amazon Advertising API, which needs its own approval separate from
        the Selling Partner API. Nothing is shown here rather than showing
        placeholder figures.
      </div>
    </>
  );
}
