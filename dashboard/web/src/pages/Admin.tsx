import { useSession } from "../lib/session";
import { isSuper } from "../lib/types";

/**
 * Users, roles and settings.
 *
 * The rules this screen will surface are already enforced in Postgres: an
 * admin can invite viewers and nothing else, nobody edits their own role, and
 * the last active super admin cannot be removed. This page will be a
 * convenience over those policies, never the thing that enforces them - which
 * is why it can be built last without leaving a hole.
 */
export default function Admin() {
  const { profile } = useSession();
  return (
    <div className="empty">
      <strong>Admin screens are not built yet.</strong>
      {isSuper(profile?.role)
        ? "As super admin you will manage users, roles and data sources here."
        : "As an admin you will be able to invite viewers here."}
      {" "}The permission rules are already live in the database, so nothing is
      unguarded in the meantime.
    </div>
  );
}
