import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import styled from "@emotion/styled";
import { api } from "../api";
import { isOpsRole, useAuth } from "../auth";
import { ConsoleChrome } from "../ConsoleChrome";
import { useSetup } from "../setup";
import { theme } from "../styles/theme";
import {
  Button,
  ClusterIcon,
  ConfirmModal,
  Err,
  FieldLabel,
  FieldRow,
  Hint,
  Input,
  LogPane,
  PairGrid,
  Page,
  PageHeader,
  PasswordInput,
  WarnBox,
} from "../ui";
import { useDeploySession } from "./DeploySession";
import { useWizard } from "./WizardContext";

const Section = styled.section`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1.05rem 1.1rem 1.15rem;
  margin: 0 0 1rem;
`;

const SectionTitle = styled.h2`
  margin: 0 0 0.5rem;
  font-size: 1.05rem;
  font-weight: 700;
  letter-spacing: -0.02em;
`;

type HaDisk = {
  ok: boolean;
  build_allowed?: boolean;
  errors: string[];
  instructions?: string;
  data_disk?: string;
  meta_disk?: string;
  will_auto_partition?: { label?: string; disk?: string; message?: string; size_human?: string }[];
};

const IPV4 =
  /^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;

function validIpv4(value: string): boolean {
  return IPV4.test(value.trim());
}

type ClusterTopologyResp = {
  cluster?: { topology?: string; nodes?: string[]; offline?: string[] };
};

export default function AddSecondServerPage() {
  const { user } = useAuth();
  const { deployed, loading: setupLoading } = useSetup();
  const { draft, save, error: draftError } = useWizard();
  const { log, message, pipelineBusy, installProgress, runHaOrchestration } = useDeploySession();
  const navigate = useNavigate();
  const ops = isOpsRole(user?.role);

  const [topologyChecked, setTopologyChecked] = useState(false);
  const [topologyOk, setTopologyOk] = useState(false);
  const [liveClusterNodes, setLiveClusterNodes] = useState<string[]>([]);

  const [peerIp, setPeerIp] = useState("");
  const [peerName, setPeerName] = useState("");
  const [obsIp, setObsIp] = useState("");
  const [vipIp, setVipIp] = useState("");
  const [rootPass, setRootPass] = useState("");
  const [rootConfirm, setRootConfirm] = useState("");
  const [adminPass, setAdminPass] = useState("");
  const [adminConfirm, setAdminConfirm] = useState("");
  const [fieldErr, setFieldErr] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [starting, setStarting] = useState(false);

  const [haDisk, setHaDisk] = useState<HaDisk | null>(null);
  const [haDiskLoading, setHaDiskLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void api<ClusterTopologyResp>("/api/cluster/status")
      .then((st) => {
        if (cancelled) return;
        setTopologyOk(st.cluster?.topology === "1vm");
        const nodes = [
          ...(st.cluster?.nodes || []),
          ...(st.cluster?.offline || []),
        ].filter(Boolean);
        setLiveClusterNodes([...new Set(nodes)]);
      })
      .catch(() => {
        if (!cancelled) {
          setTopologyOk(false);
          setLiveClusterNodes([]);
        }
      })
      .finally(() => {
        if (!cancelled) setTopologyChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function checkDisks() {
    setHaDiskLoading(true);
    void api<HaDisk>("/api/wizard/ha-disk-preflight")
      .then(setHaDisk)
      .catch((err) =>
        setHaDisk({
          ok: false,
          errors: [err instanceof Error ? err.message : "Could not check disks"],
        }),
      )
      .finally(() => setHaDiskLoading(false));
  }

  useEffect(() => {
    if (!topologyChecked || !topologyOk) return;
    checkDisks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topologyChecked, topologyOk]);

  if (!setupLoading && !deployed) {
    return <Navigate to="/wizard" replace />;
  }
  if (!ops) {
    return <Navigate to="/cluster" replace />;
  }
  if (topologyChecked && !topologyOk && !pipelineBusy) {
    return <Navigate to="/cluster" replace />;
  }

  const haDiskBlocked = haDisk != null && !haDisk.ok && haDisk.build_allowed !== true;
  const replacingCreds = !!(rootPass || adminPass);
  const activeRun = pipelineBusy;
  const { current, total, label, complete, failed } = installProgress;

  function validate(): string {
    if (!peerIp.trim()) return "Enter the second server's IPv4 address.";
    if (!validIpv4(peerIp)) return "Second server IP must be an IPv4 address.";
    if (!obsIp.trim()) return "Enter the Observability VM IPv4 address.";
    if (!validIpv4(obsIp)) return "Observability VM IP must be an IPv4 address.";
    if (!vipIp.trim()) return "Enter the cluster VIP (unused IPv4, not a mail-node address).";
    if (!validIpv4(vipIp)) return "Cluster VIP must be an IPv4 address.";
    const vip = vipIp.trim();
    if (vip === peerIp.trim()) return "Cluster VIP must not be the second server IP.";
    if (vip === obsIp.trim()) return "Cluster VIP must not be the Observability VM IP.";
    if (draft.local_host_ip && vip === draft.local_host_ip.trim()) {
      return "Cluster VIP must not be this server's own address.";
    }
    if (!draft.host_credentials_set && (!rootPass || !adminPass)) {
      return "Enter the root and admin passwords for the new server and the Observability VM.";
    }
    if (replacingCreds) {
      if (!rootPass || !adminPass) return "Enter both passwords to set provisioning credentials.";
      if (rootPass.length < 8) return "Root password must be at least 8 characters.";
      if (adminPass.length < 8) return "Admin password must be at least 8 characters.";
      if (rootPass !== rootConfirm) return "Root password and confirmation do not match.";
      if (adminPass !== adminConfirm) return "Admin password and confirmation do not match.";
    }
    if (haDiskBlocked) return "Resolve the DRBD disk issue below before continuing.";
    return "";
  }

  function openConfirm() {
    const err = validate();
    if (err) {
      setFieldErr(err);
      return;
    }
    setFieldErr("");
    setConfirmOpen(true);
  }

  async function start() {
    setStarting(true);
    try {
      await save({
        topology: "2vm",
        peer_host_ip: peerIp.trim(),
        peer_host_name: peerName.trim(),
        observability_vm_ip: obsIp.trim(),
        cluster_vip_ip: vipIp.trim(),
        ...(replacingCreds ? { host_root_pass: rootPass, kin_user_pass: adminPass } : {}),
        current_step: "deploy",
      });
    } catch (err) {
      setFieldErr(err instanceof Error ? err.message : "Could not save the new server details.");
      setStarting(false);
      setConfirmOpen(false);
      return;
    }
    setConfirmOpen(false);
    await runHaOrchestration({ confirmed: true });
    setStarting(false);
  }

  const showForm = !activeRun && !(complete && !failed);

  return (
    <ConsoleChrome subtitle="Add a second server">
      <Page>
        <PageHeader
          icon={<ClusterIcon />}
          title="Add a second server"
          subtitle="This server keeps its domain, TLS, directory, and mailbox settings exactly as they are. Only the new server's own connection details are needed below."
        />
        {!showForm ? (
          <Section>
            <SectionTitle>{complete && !failed ? "Done" : failed ? "Did not finish" : "In progress"}</SectionTitle>
            <Hint>
              {complete && !failed
                ? "The second server has joined as an HA pair. Mail kept running on this server the whole time."
                : failed
                  ? "The HA build did not finish cleanly. Nothing on this server's existing mail was touched; review the log below before retrying."
                  : `${current}/${total || "?"} ${label}`}
            </Hint>
            <LogPane aria-label="Add second server log">{log}</LogPane>
            {message ? <Hint>{message}</Hint> : null}
            {complete || failed ? (
              <Button type="button" onClick={() => navigate("/cluster")}>
                Back to Cluster
              </Button>
            ) : null}
          </Section>
        ) : (
          <>
            {liveClusterNodes.length > 0 ? (
              <WarnBox role="status">
                This server already has a live cluster membership
                ({liveClusterNodes.join(", ")}). Build HA pair is for empty
                nodes only and will refuse rather than rewrite production
                Pacemaker state. After Remove host, keep running as a
                single-node cluster, or use the add-host playbook to attach a
                blank peer. Do not start Build HA pair against this survivor.
              </WarnBox>
            ) : null}
            <Section>
              <SectionTitle>New server</SectionTitle>
              <Hint>
                Domain, TLS, directory sign-in, mobile email, and mailbox seats already configured
                on this server are reused as-is. They are not asked again here.
              </Hint>
              <FieldRow>
                <FieldLabel htmlFor="peer_ip" required>
                  Second server IP
                </FieldLabel>
                <Input
                  id="peer_ip"
                  value={peerIp}
                  onChange={(e) => {
                    setFieldErr("");
                    setPeerIp(e.target.value);
                  }}
                  placeholder="192.0.2.14"
                  autoComplete="off"
                  disabled={starting}
                />
              </FieldRow>
              <FieldRow>
                <FieldLabel htmlFor="peer_name" optional>
                  Second server hostname
                </FieldLabel>
                <Input
                  id="peer_name"
                  value={peerName}
                  onChange={(e) => setPeerName(e.target.value)}
                  placeholder="mail2.example.co.id"
                  autoComplete="off"
                  disabled={starting}
                />
              </FieldRow>
              <FieldRow>
                <FieldLabel htmlFor="obs_ip" required>
                  Observability VM IP
                </FieldLabel>
                <Input
                  id="obs_ip"
                  value={obsIp}
                  onChange={(e) => {
                    setFieldErr("");
                    setObsIp(e.target.value);
                  }}
                  placeholder="192.0.2.12"
                  autoComplete="off"
                  disabled={starting}
                />
                <Hint>
                  Independent witness for quorum (qdevice) plus fencing. A 2-server HA pair is not
                  complete without it, so it is provisioned in this same run.
                </Hint>
              </FieldRow>
              <FieldRow>
                <FieldLabel htmlFor="vip_ip" required>
                  Cluster VIP
                </FieldLabel>
                <Input
                  id="vip_ip"
                  value={vipIp}
                  onChange={(e) => {
                    setFieldErr("");
                    setVipIp(e.target.value);
                  }}
                  placeholder="192.0.2.16"
                  autoComplete="off"
                  disabled={starting}
                />
                <Hint>
                  Unused IPv4 that Pacemaker floats to whichever node is Promoted. Must not be this
                  server, the second server, or the Observability VM.
                  {draft.local_host_ip ? ` This server is ${draft.local_host_ip}.` : ""}
                </Hint>
              </FieldRow>
            </Section>

            <Section>
              <SectionTitle>Provisioning credentials</SectionTitle>
              {draft.host_credentials_set ? (
                <Hint>
                  Credentials are already stored from an earlier attempt. Leave the fields empty to
                  reuse them, or enter new values to replace.
                </Hint>
              ) : (
                <Hint>
                  Must already work over SSH on the new mail server and the Observability VM: the
                  root password, and the password for Linux user kin (sudo).
                </Hint>
              )}
              <PairGrid>
                <FieldRow>
                  <FieldLabel htmlFor="root_pass" required={!draft.host_credentials_set}>
                    Root password
                  </FieldLabel>
                  <PasswordInput
                    id="root_pass"
                    autoComplete="new-password"
                    value={rootPass}
                    onChange={(e) => {
                      setFieldErr("");
                      setRootPass(e.target.value);
                    }}
                    disabled={starting}
                  />
                </FieldRow>
                <FieldRow>
                  <FieldLabel htmlFor="root_pass_confirm" required={!draft.host_credentials_set}>
                    Confirm root password
                  </FieldLabel>
                  <PasswordInput
                    id="root_pass_confirm"
                    autoComplete="new-password"
                    value={rootConfirm}
                    onChange={(e) => {
                      setFieldErr("");
                      setRootConfirm(e.target.value);
                    }}
                    disabled={starting}
                  />
                </FieldRow>
                <FieldRow>
                  <FieldLabel htmlFor="admin_pass" required={!draft.host_credentials_set}>
                    Admin password
                  </FieldLabel>
                  <PasswordInput
                    id="admin_pass"
                    autoComplete="new-password"
                    value={adminPass}
                    onChange={(e) => {
                      setFieldErr("");
                      setAdminPass(e.target.value);
                    }}
                    disabled={starting}
                  />
                </FieldRow>
                <FieldRow>
                  <FieldLabel htmlFor="admin_pass_confirm" required={!draft.host_credentials_set}>
                    Confirm admin password
                  </FieldLabel>
                  <PasswordInput
                    id="admin_pass_confirm"
                    autoComplete="new-password"
                    value={adminConfirm}
                    onChange={(e) => {
                      setFieldErr("");
                      setAdminConfirm(e.target.value);
                    }}
                    disabled={starting}
                  />
                </FieldRow>
              </PairGrid>
              <Hint>At least 8 characters each. Confirmation must match.</Hint>
            </Section>

            <Section>
              <SectionTitle>Disk check</SectionTitle>
              {haDiskLoading ? (
                <Hint>Checking second-disk partitions on both mail servers...</Hint>
              ) : haDisk && !haDisk.ok && haDisk.build_allowed !== true ? (
                <WarnBox>
                  <strong>Second disk not ready for DRBD.</strong> This stays blocked until both
                  mail VMs have a unique blank spare disk (or the proven GPT layout).
                  <ul style={{ margin: "0.5rem 0 0", paddingLeft: "1.2rem" }}>
                    {(haDisk.errors || []).map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                  {haDisk.instructions ? (
                    <p style={{ margin: "0.65rem 0 0" }}>{haDisk.instructions}</p>
                  ) : null}
                </WarnBox>
              ) : haDisk?.ok ? (
                <Hint>
                  DRBD disks look ready
                  {haDisk.data_disk && haDisk.meta_disk
                    ? ` (${haDisk.data_disk} data, ${haDisk.meta_disk} meta)`
                    : ""}
                  .
                </Hint>
              ) : (
                <Hint>Disk status unavailable yet.</Hint>
              )}
              <Button type="button" variant="secondary" onClick={checkDisks} disabled={haDiskLoading || starting}>
                Recheck disks
              </Button>
            </Section>

            <Err>{fieldErr || draftError}</Err>
            <Button type="button" loading={starting} disabled={starting} onClick={openConfirm}>
              Continue
            </Button>
          </>
        )}

        <ConfirmModal
          open={confirmOpen}
          title="Add a second server"
          message={
            <>
              Install Zimbra and cluster software on <strong>{peerIp || "the new server"}</strong>,
              provision <strong>{obsIp || "the Observability VM"}</strong> as the qdevice/fencing
              witness, then form a DRBD/Pacemaker HA pair with this server using VIP{" "}
              <strong>{vipIp || "unset"}</strong>.
            </>
          }
          detail="This server's own mail, domain, TLS, and directory settings are not changed. Mail delivery keeps running on this server throughout."
          confirmLabel="Start"
          loading={starting}
          onCancel={() => {
            if (starting) return;
            setConfirmOpen(false);
          }}
          onConfirm={() => void start()}
        />
      </Page>
    </ConsoleChrome>
  );
}
