import { useCallback, useEffect, useState } from "react";
import { supabase } from "../lib/supabase";
import { useSession } from "../lib/session";
import { agoWords } from "../lib/format";
import { isSuper } from "../lib/types";
import type { AuditEntry, Invite, Profile, Role } from "../lib/types";

/**
 * Users, invites and the trail of who did what.
 *
 * Every rule this screen appears to apply is actually enforced in Postgres by
 * the policies and triggers in supabase/migrations: an admin may invite
 * viewers and nothing else, nobody edits their own role, and the last active
 * super admin cannot be demoted or deactivated. Hiding a control here is a
 * courtesy, not a control - a viewer who calls the same endpoint from a
 * console still gets nothing.
 *
 * Which is why failures are shown verbatim rather than swallowed. When the
 * database refuses something, the message it gives is the real rule, and
 * paraphrasing it here would let this page and the policies drift apart.
 */
const ROLE_LABEL: Record<Role, string> = {
  viewer: "Viewer",
  admin: "Admin",
  super_admin: "Super admin",
};

export default function Admin() {
  const { profile } = useSession();
  const iAmSuper = isSuper(profile?.role);

  const [people, setPeople] = useState<Profile[] | null>(null);
  const [invites, setInvites] = useState<Invite[] | null>(null);
  const [log, setLog] = useState<AuditEntry[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const [p, i, a] = await Promise.all([
      supabase.from("profiles")
        .select("id, email, full_name, role, is_active").order("email"),
      supabase.from("invites")
        .select("email, role, created_at, claimed_at")
        .is("claimed_at", null).order("created_at", { ascending: false }),
      supabase.from("audit_log")
        .select("id, actor_email, action, target_type, target_id, detail, created_at")
        .order("created_at", { ascending: false }).limit(50),
    ]);
    setPeople((p.data ?? []) as Profile[]);
    setInvites((i.data ?? []) as Invite[]);
    setLog((a.data ?? []) as AuditEntry[]);
    const first = p.error ?? i.error ?? a.error;
    setErr(first ? first.message : null);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => PromiseLike<{ error: { message: string } | null }>,
                     ok: string) => {
    setBusy(true);
    setErr(null);
    setNote(null);
    const { error } = await fn();
    if (error) setErr(error.message);
    else setNote(ok);
    await load();
    setBusy(false);
  };

  const invite = () => {
    const addr = email.trim().toLowerCase();
    if (!addr) return;
    void act(
      () => supabase.from("invites").insert({ email: addr, role }),
      `Invited ${addr} as ${ROLE_LABEL[role].toLowerCase()}. They get access by ` +
      `signing up with that address.`,
    ).then(() => { setEmail(""); setRole("viewer"); });
  };

  return (
    <>
      {err && <div className="banner bad">{err}</div>}
      {note && <div className="banner">{note}</div>}

      <h2 className="section-head">Invite someone</h2>
      <div className="controls">
        <div className="field">
          <label htmlFor="invite-email">Email</label>
          <input
            id="invite-email"
            type="email"
            value={email}
            placeholder="name@dcgnorthamerica.com"
            style={{ minWidth: "16rem" }}
            onChange={(e) => setEmail(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") invite(); }}
          />
        </div>
        <div className="field">
          <label htmlFor="invite-role">Role</label>
          <select id="invite-role" value={role}
                  onChange={(e) => setRole(e.target.value as Role)}>
            <option value="viewer">Viewer</option>
            {/* Offered only to a super admin because only a super admin can
                do it. An admin who tries anyway is refused by the database. */}
            {iAmSuper && <option value="admin">Admin</option>}
            {iAmSuper && <option value="super_admin">Super admin</option>}
          </select>
        </div>
        <button className="btn" disabled={busy || !email.trim()} onClick={invite}>
          Record invite
        </button>
      </div>
      <p className="footnote">
        An invite does not send an email — it records that this address may sign
        up, and with which role. Send them the link yourself; when they sign up
        with that address they are provisioned automatically. Anyone signing up
        without an invite gets an account that can see nothing.
      </p>

      <h2 className="section-head">Pending invites</h2>
      {invites === null ? (
        <div className="loading">Loading…</div>
      ) : invites.length === 0 ? (
        <div className="empty"><strong>No one is waiting to sign up.</strong></div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Invited</th>
              <th className="num"></th>
            </tr>
          </thead>
          <tbody>
            {invites.map((i) => (
              <tr key={i.email}>
                <td className="name">{i.email}</td>
                <td className="left">{ROLE_LABEL[i.role]}</td>
                <td className="left">{agoWords(i.created_at)}</td>
                <td className="num">
                  <button
                    className="btn ghost"
                    disabled={busy}
                    onClick={() =>
                      void act(
                        () => supabase.from("invites").delete()
                          .eq("email", i.email),
                        `Revoked the invite for ${i.email}.`,
                      )
                    }
                  >
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="section-head">People</h2>
      {people === null ? (
        <div className="loading">Loading…</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Person</th>
              <th>Role</th>
              <th>Status</th>
              <th className="num"></th>
            </tr>
          </thead>
          <tbody>
            {people.map((p) => {
              const isMe = p.id === profile?.id;
              return (
                <tr key={p.id}>
                  <td className="name">
                    {p.full_name || p.email}
                    <small>{p.full_name ? p.email : ""}{isMe ? " · you" : ""}</small>
                  </td>
                  <td className="left">
                    {iAmSuper && !isMe ? (
                      <select
                        value={p.role}
                        disabled={busy}
                        onChange={(e) =>
                          void act(
                            () => supabase.from("profiles")
                              .update({ role: e.target.value as Role })
                              .eq("id", p.id),
                            `Updated ${p.email} to ${ROLE_LABEL[e.target.value as Role].toLowerCase()}.`,
                          )
                        }
                      >
                        <option value="viewer">Viewer</option>
                        <option value="admin">Admin</option>
                        <option value="super_admin">Super admin</option>
                      </select>
                    ) : (
                      // Nobody edits their own role, super admin included.
                      // That is a database trigger, not a disabled control.
                      ROLE_LABEL[p.role]
                    )}
                  </td>
                  <td className="left">{p.is_active ? "Active" : "Deactivated"}</td>
                  <td className="num">
                    {iAmSuper && !isMe && (
                      <button
                        className="btn ghost"
                        disabled={busy}
                        onClick={() =>
                          void act(
                            () => supabase.from("profiles")
                              .update({ is_active: !p.is_active })
                              .eq("id", p.id),
                            p.is_active
                              ? `Deactivated ${p.email}. They can no longer sign in.`
                              : `Reactivated ${p.email}. They have access again.`,
                          )
                        }
                      >
                        {p.is_active ? "Deactivate" : "Reactivate"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <p className="footnote">
        Access is removed by deactivating, not deleting. A deleted row takes the
        audit trail's references with it, and the question worth answering later
        is who had access in March, not who has it today.
      </p>

      <h2 className="section-head">Recent activity</h2>
      {log === null ? (
        <div className="loading">Loading…</div>
      ) : log.length === 0 ? (
        <div className="empty"><strong>Nothing has happened yet.</strong></div>
      ) : (
        <table>
          <tbody>
            {log.map((e) => (
              <tr key={e.id}>
                <td className="name" style={{ width: "12rem" }}>
                  {e.actor_email ?? "system"}
                  <small>{agoWords(e.created_at)}</small>
                </td>
                <td className="left mono">{e.action}</td>
                <td className="left mono">{e.target_id ?? ""}</td>
                <td className="left mono">
                  {Object.keys(e.detail ?? {}).length > 0
                    ? JSON.stringify(e.detail)
                    : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
