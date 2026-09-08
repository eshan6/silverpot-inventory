import { Freshness } from "../components/Freshness";
import type { Marketplace } from "../lib/types";

/**
 * Ad spend over units actually sold - the question this dashboard exists for.
 *
 * The arithmetic already lives in the spend_daily view, so that spend is
 * summed across every ad program and units come from orders rather than from
 * attribution. This page renders that view once ads ingestion exists; until
 * then the numerator is missing and a cost-per-unit built from half the inputs
 * would read better than reality, which is the wrong direction to be wrong in.
 */
export default function Spend({ marketplace }: { marketplace: Marketplace }) {
  return (
    <>
      <Freshness marketplace={marketplace} />
      <div className="empty">
        <strong>Waiting on ad spend.</strong>
        Cost per unit needs ad spend as its numerator, and that comes from the
        Advertising API. Units sold are already being collected, so this page
        completes itself the moment ads ingestion lands.
      </div>
    </>
  );
}
