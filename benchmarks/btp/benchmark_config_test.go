package benchmarks

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

type presentationFile struct {
	Measurements []struct {
		ID          string            `yaml:"id"`
		Label       string            `yaml:"label"`
		Source      string            `yaml:"source"`
		Metric      string            `yaml:"metric"`
		Match       map[string]string `yaml:"match"`
		Statistic   string            `yaml:"statistic"`
		Unit        string            `yaml:"unit"`
		DisplayUnit string            `yaml:"display_unit"`
	} `yaml:"measurements"`
}

type dashboardFile struct {
	Kind string `yaml:"kind"`
	Spec struct {
		Panels  map[string]dashboardPanel `yaml:"panels"`
		Layouts []struct {
			Spec struct {
				Items []struct {
					Content struct {
						Ref string `yaml:"$ref"`
					} `yaml:"content"`
				} `yaml:"items"`
			} `yaml:"spec"`
		} `yaml:"layouts"`
	} `yaml:"spec"`
}

type dashboardPanel struct {
	Kind string `yaml:"kind"`
	Spec struct {
		Display struct {
			Name string `yaml:"name"`
		} `yaml:"display"`
		Plugin struct {
			Kind string `yaml:"kind"`
			Spec struct {
				YAxis struct {
					Format struct {
						Unit string `yaml:"unit"`
					} `yaml:"format"`
				} `yaml:"yAxis"`
			} `yaml:"spec"`
		} `yaml:"plugin"`
		Queries []struct {
			Kind string `yaml:"kind"`
			Spec struct {
				Plugin struct {
					Kind string `yaml:"kind"`
					Spec struct {
						Query            string `yaml:"query"`
						SeriesNameFormat string `yaml:"seriesNameFormat"`
					} `yaml:"spec"`
				} `yaml:"plugin"`
			} `yaml:"spec"`
		} `yaml:"queries"`
	} `yaml:"spec"`
}

func TestBenchmarkDashboardSparseLifecycleMeasurements(t *testing.T) {
	root := filepath.Join("..", "..")
	content, err := os.ReadFile(filepath.Join(root, "benchmarks/btp/perses/overview.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var dashboard dashboardFile
	if err := yaml.Unmarshal(content, &dashboard); err != nil {
		t.Fatalf("parse dashboard YAML: %v", err)
	}
	if dashboard.Kind != "Dashboard" || len(dashboard.Spec.Panels) == 0 {
		t.Fatalf("invalid dashboard header or empty panels: kind=%q panels=%d", dashboard.Kind, len(dashboard.Spec.Panels))
	}

	queries := make(map[string]string)
	titles := make(map[string]bool)
	for id, panel := range dashboard.Spec.Panels {
		titles[panel.Spec.Display.Name] = true
		if panel.Kind != "Panel" {
			t.Errorf("panel %s has unsupported kind %q", id, panel.Kind)
		}
		for _, query := range panel.Spec.Queries {
			if query.Kind != "TimeSeriesQuery" || query.Spec.Plugin.Kind != "PrometheusTimeSeriesQuery" {
				t.Errorf("panel %s query uses unsupported discovery type %q/%q", id, query.Kind, query.Spec.Plugin.Kind)
				continue
			}
			queries[id] = query.Spec.Plugin.Spec.Query
		}
	}
	for _, obsolete := range []string{"Iterations per second", "Time to update"} {
		if titles[obsolete] {
			t.Errorf("obsolete sparse or unsupported panel %q remains", obsolete)
		}
	}
	for id, metric := range map[string]string{
		"0_0": "xp_diadromos_k6_iteration_duration_mean{scenario=\"create_delete\"}",
		"0_1": "xp_diadromos_k6_iterations_total{scenario=\"create_delete\"}",
		"2_0": "xp_diadromos_k6_xp_time_to_ready_mean{scenario=\"create_delete\"}",
		"2_2": "xp_diadromos_k6_xp_time_to_delete_mean{scenario=\"create_delete\"}",
	} {
		if got := queries[id]; got != metric {
			t.Errorf("panel %s query = %q, want %q", id, got, metric)
		}
	}
	if strings.Contains(string(content), "rate(xp_diadromos_k6_iteration_duration") || strings.Contains(string(content), "xp_time_to_update") {
		t.Error("dashboard retains sparse iteration rates or unsupported update metrics")
	}
	if dashboard.Spec.Panels["0_0"].Spec.Plugin.Spec.YAxis.Format.Unit != "milliseconds" {
		t.Error("iteration duration axis must be milliseconds")
	}
	for _, id := range []string{"2_0", "2_2"} {
		if dashboard.Spec.Panels[id].Spec.Plugin.Spec.YAxis.Format.Unit != "milliseconds" {
			t.Errorf("lifecycle panel %s axis must be milliseconds", id)
		}
	}

	referenced := make(map[string]int)
	for _, layout := range dashboard.Spec.Layouts {
		for _, item := range layout.Spec.Items {
			if !strings.HasPrefix(item.Content.Ref, "#/spec/panels/") {
				t.Errorf("layout has invalid panel ref %q", item.Content.Ref)
				continue
			}
			id := strings.TrimPrefix(item.Content.Ref, "#/spec/panels/")
			if _, ok := dashboard.Spec.Panels[id]; !ok {
				t.Errorf("layout references missing panel %q", id)
			}
			referenced[id]++
		}
	}
	for id := range dashboard.Spec.Panels {
		if referenced[id] != 1 {
			t.Errorf("panel %s has %d layout references; want exactly one", id, referenced[id])
		}
	}

	controllers := []string{
		"managed/subaccount.account.btp.sap.crossplane.io",
		"managed/directory.account.btp.sap.crossplane.io",
		"managed/entitlement.account.btp.sap.crossplane.io",
		"managed/account.btp.sap.crossplane.io/v1alpha1, kind=directoryentitlement",
		"managed/security.btp.sap.crossplane.io/v1alpha1, kind=subaccountapicredential",
	}
	for id, fragments := range map[string][]string{
		"4_0": {"controller_runtime_reconcile_time_seconds_sum", "controller_runtime_reconcile_time_seconds_count", `job="provider"`, "archive_filename", "[5m]"},
		"4_1": {"workqueue_depth", `job="provider"`, "archive_filename"},
		"4_2": {"workqueue_queue_duration_seconds_sum", "workqueue_queue_duration_seconds_count", `job="provider"`, "archive_filename", "[5m]"},
		"5_0": {"xp_diadromos_k6_xp_lifecycle_phase_duration_mean", "create_request|delete_request"},
		"5_1": {"xp_diadromos_k6_xp_lifecycle_phase_duration_mean", "readiness|kubernetes_absence_wait"},
		"5_2": {"upjet_resource_ext_api_duration_sum", "upjet_resource_ext_api_duration_count", `job="provider"`, "operation", "archive_filename", "[5m]"},
	} {
		query, ok := queries[id]
		if !ok {
			t.Errorf("missing diagnostic panel %s", id)
			continue
		}
		for _, fragment := range fragments {
			if !strings.Contains(query, fragment) {
				t.Errorf("panel %s query %q lacks %q", id, query, fragment)
			}
		}
		if id == "5_0" || id == "5_1" {
			format := dashboard.Spec.Panels[id].Spec.Queries[0].Spec.Plugin.Spec.SeriesNameFormat
			for _, label := range []string{"{{resource_kind}}", "{{stage}}", "{{outcome}}", "{{archive_filename}}"} {
				if !strings.Contains(format, label) {
					t.Errorf("panel %s legend %q lacks %q", id, format, label)
				}
			}
		}
		if strings.Contains(query, "_sum") || strings.Contains(query, "_count") {
			if strings.Contains(query, "or vector(0)") || strings.Contains(query, "+ 0") {
				t.Errorf("panel %s must not fill zero-count histogram windows with zero", id)
			}
		}
	}
	for _, id := range []string{"4_0", "4_1", "4_2"} {
		query := queries[id]
		for _, controller := range controllers {
			escaped := strings.ReplaceAll(controller, ".", "[.]")
			if !strings.Contains(query, escaped) {
				t.Errorf("panel %s excludes exact exercised controller %q", id, controller)
			}
		}
		if !strings.Contains(query, `controller=~"^(`) || !strings.Contains(query, `)$"`) {
			t.Errorf("panel %s controller selector must be anchored", id)
		}
	}
	for _, id := range []string{"4_0", "4_2", "5_2"} {
		if dashboard.Spec.Panels[id].Spec.Plugin.Spec.YAxis.Format.Unit != "seconds" {
			t.Errorf("panel %s must use a seconds axis", id)
		}
	}
	for _, id := range []string{"5_0", "5_1"} {
		if dashboard.Spec.Panels[id].Spec.Plugin.Spec.YAxis.Format.Unit != "milliseconds" {
			t.Errorf("panel %s must use a milliseconds axis", id)
		}
	}
	if strings.Contains(queries["5_2"], "resource_kind") || strings.Contains(queries["5_2"], "controller") {
		t.Error("external-operation duration must not claim resource-kind or controller attribution")
	}
}

func TestBenchmarkPresentationContract(t *testing.T) {
	root := filepath.Join("..", "..")
	content, err := os.ReadFile(filepath.Join(root, "benchmarks/btp/report-presentation.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var presentation presentationFile
	if err := yaml.Unmarshal(content, &presentation); err != nil {
		t.Fatalf("parse presentation YAML: %v", err)
	}
	if len(presentation.Measurements) != 28 {
		t.Fatalf("presentation has %d rows; want 28", len(presentation.Measurements))
	}
	wantControllers := []string{
		"managed/subaccount.account.btp.sap.crossplane.io",
		"managed/directory.account.btp.sap.crossplane.io",
		"managed/entitlement.account.btp.sap.crossplane.io",
		"managed/account.btp.sap.crossplane.io/v1alpha1, kind=directoryentitlement",
		"managed/security.btp.sap.crossplane.io/v1alpha1, kind=subaccountapicredential",
	}
	kinds := []string{"Subaccount", "Directory", "Entitlement", "DirectoryEntitlement", "SubaccountApiCredential"}
	errorRows := 0
	for i, row := range presentation.Measurements {
		if i < 4 && row.DisplayUnit == "" {
			t.Errorf("CPU/memory row %s lost its supported display_unit", row.ID)
		}
		if row.Unit == "ms" && row.DisplayUnit != "duration" {
			t.Errorf("duration row %s lacks human-readable display_unit", row.ID)
		}
		if row.Source == "raw_k6" && (row.Metric == "xp_time_to_ready" || row.Metric == "xp_time_to_delete") && row.Match["scenario"] != "create_delete" {
			t.Errorf("lifecycle row %s lacks create_delete match", row.ID)
		}
		if strings.Contains(row.ID, "reconcile-errors") {
			errorRows++
			kindIndex := i - 23
			if kindIndex < 0 || kindIndex >= len(kinds) {
				t.Fatalf("unexpected reconciliation error row order at %d: %s", i, row.ID)
			}
			if row.Source != "tsdb" || row.Metric != "controller_runtime_reconcile_errors_total" || row.Statistic != "observed_increase" || row.Unit != "count" {
				t.Errorf("invalid reconciliation error row %s", row.ID)
			}
			if row.Match["job"] != "provider" || row.Match["namespace"] != "crossplane-system" || row.Match["controller"] != wantControllers[kindIndex] {
				t.Errorf("row %s has incorrect controller selector: %#v", row.ID, row.Match)
			}
		}
	}
	if errorRows != 5 {
		t.Errorf("found %d reconciliation-error rows; want 5", errorRows)
	}
	for i, kind := range kinds {
		mean, median := presentation.Measurements[5+i*3], presentation.Measurements[6+i*3]
		if mean.Match["resource_kind"] != kind || median.Match["resource_kind"] != kind || median.Statistic != "p50" || mean.Unit != "ms" || median.Unit != "ms" || mean.DisplayUnit != "duration" || median.DisplayUnit != "duration" {
			t.Errorf("readiness mean/median pairing incorrect for %s", kind)
		}
	}
	if row := presentation.Measurements[4]; row.ID != "iteration-duration-mean" || row.Source != "raw_k6" || row.Metric != "iteration_duration" || row.Match["scenario"] != "create_delete" || row.Statistic != "mean" || row.Unit != "ms" || row.DisplayUnit != "duration" {
		t.Errorf("invalid complete-iteration duration row: %#v", row)
	}
}

func isLowerHex(s string) bool {
	decoded, err := hex.DecodeString(s)
	return err == nil && len(decoded)*2 == len(s) && s == strings.ToLower(s)
}

func TestBenchmarkWorkflowToolPins(t *testing.T) {
	root := filepath.Join("..", "..")
	content, err := os.ReadFile(filepath.Join(root, ".github/workflows/run-btp-benchmark.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var workflow struct {
		Jobs map[string]struct {
			Outputs map[string]string `yaml:"outputs"`
			Steps   []struct {
				Name string            `yaml:"name"`
				Uses string            `yaml:"uses"`
				With map[string]string `yaml:"with"`
				Env  map[string]string `yaml:"env"`
			} `yaml:"steps"`
		} `yaml:"jobs"`
	}
	if err := yaml.Unmarshal(content, &workflow); err != nil {
		t.Fatalf("parse benchmark workflow: %v", err)
	}
	var actionCheckout, execution, report, preflight bool
	for _, step := range workflow.Jobs["benchmark"].Steps {
		switch {
		case step.Name == "Check out pinned xp-diadromos actions":
			actionCheckout = step.With["repository"] == "gergely-szabo-sap/xp-diadromos" &&
				step.With["ref"] == "16fe4521237bd8217475b418a4e5f7c25b4c9598"
		case strings.HasSuffix(step.Uses, "/run-test"):
			execution = step.With["version"] == "v0.9.4"
		case strings.HasSuffix(step.Uses, "/metrics-ci-report"):
			report = step.With["version"] == "v0.9.4" && step.With["input"] == "${{ steps.test.outputs.archive }}"
		case step.Name == "Install CLI for baseline preflight":
			preflight = step.Env["XP_METRICS_CI_VERSION"] == "v0.9.4"
		}
	}
	configBytes, err := os.ReadFile(filepath.Join(root, "benchmarks/btp/config.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var config struct {
		K6 struct {
			CustomImage string `yaml:"custom_image"`
		} `yaml:"k6"`
	}
	if err := yaml.Unmarshal(configBytes, &config); err != nil {
		t.Fatalf("parse benchmark config: %v", err)
	}
	k6Image := config.K6.CustomImage == "ghcr.io/gergely-szabo-sap/xp-diadromos-k6:v0.9.4"
	modeOutput := strings.Contains(workflow.Jobs["benchmark"].Outputs["report_mode"], "baseline-validation") &&
		strings.Contains(workflow.Jobs["benchmark"].Outputs["report_mode"], "current-only")
	workflowText := string(content)
	baselineWiring := !strings.Contains(workflowText, "actions: read") &&
		strings.Contains(workflowText, "baseline-artifact.py resolve") &&
		strings.Contains(workflowText, "baseline-artifact.py validate") &&
		strings.Contains(workflowText, "baseline: ${{ steps.baseline-validation.outputs.baseline_path }}") &&
		strings.Contains(workflowText, "baseline-ref.json")
	if !actionCheckout || !execution || !report || !preflight || !k6Image || !modeOutput || !baselineWiring {
		t.Fatalf("unexpected benchmark tool selection/report mode: checkout=%v execution=%v report=%v preflight=%v k6-image=%v dynamic-mode-output=%v baseline-wiring=%v", actionCheckout, execution, report, preflight, k6Image, modeOutput, baselineWiring)
	}
	refBytes, err := os.ReadFile(filepath.Join(root, "benchmarks/btp/baseline-ref.json"))
	if err != nil {
		t.Fatalf("read designated baseline reference: %v", err)
	}
	var ref struct {
		SchemaVersion string `json:"schema_version"`
		Baseline      *struct {
			ArchivePath         string `json:"archive_path"`
			RunID               int64  `json:"run_id"`
			RunAttempt          int64  `json:"run_attempt"`
			ArtifactID          int64  `json:"artifact_id"`
			ArtifactExpiresAt   string `json:"artifact_expires_at"`
			HeadSHA             string `json:"head_sha"`
			ArchiveSHA256       string `json:"archive_sha256"`
			ContractSHA256      string `json:"contract_sha256"`
			ValidatedCLI        string `json:"validated_cli"`
			EnvironmentRevision string `json:"environment_revision"`
		} `json:"baseline"`
	}
	if err := json.Unmarshal(refBytes, &ref); err != nil {
		t.Fatalf("parse designated baseline reference: %v", err)
	}
	if ref.SchemaVersion != "v3" || ref.Baseline == nil {
		t.Fatalf("designated baseline reference must use schema v3 and configure a baseline: %s", refBytes)
	}
	baseline := ref.Baseline
	if baseline.ArchivePath != "benchmarks/btp/baseline/btp-synthetic-benchmark.tsdb.tar.zst" ||
		baseline.ArtifactExpiresAt != "2026-10-14T08:54:38Z" || baseline.RunID != 37594310305 || baseline.RunAttempt != 1 || baseline.ArtifactID != 11470154615 ||
		baseline.HeadSHA != "6b148994589053b96ec719a7821cc6f73f55bf75" || baseline.EnvironmentRevision != "btp-benchmark-env-v1" ||
		len(baseline.HeadSHA) != 40 || !isLowerHex(baseline.HeadSHA) ||
		len(baseline.ArchiveSHA256) != 64 || !isLowerHex(baseline.ArchiveSHA256) ||
		len(baseline.ContractSHA256) != 64 || !isLowerHex(baseline.ContractSHA256) ||
		baseline.ValidatedCLI != "v0.9.4" || baseline.EnvironmentRevision == "" {
		t.Fatalf("designated baseline reference has invalid identity or provenance: %s", refBytes)
	}
	archivePath := filepath.Join(root, filepath.FromSlash(baseline.ArchivePath))
	archiveBytes, err := os.ReadFile(archivePath)
	if err != nil {
		t.Fatalf("read checked-in baseline archive: %v", err)
	}
	archiveDigest := sha256.Sum256(archiveBytes)
	if hex.EncodeToString(archiveDigest[:]) != baseline.ArchiveSHA256 {
		t.Fatal("checked-in baseline archive does not match its descriptor SHA-256")
	}
}

func TestBenchmarkCommentSanitizerInlineStep(t *testing.T) {
	root := filepath.Join("..", "..")
	workflowBytes, err := os.ReadFile(filepath.Join(root, ".github/workflows/run-btp-benchmark.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var workflow struct {
		Jobs map[string]struct {
			Steps []struct {
				Name string `yaml:"name"`
				Run  string `yaml:"run"`
			} `yaml:"steps"`
		} `yaml:"jobs"`
	}
	if err := yaml.Unmarshal(workflowBytes, &workflow); err != nil {
		t.Fatalf("parse workflow YAML: %v", err)
	}
	var script string
	for _, step := range workflow.Jobs["publish-archive-comment"].Steps {
		if step.Name == "Prepare safe measurement table" {
			script = step.Run
		}
	}
	if script == "" || strings.Contains(script, "gh api") || strings.Contains(script, "Publish benchmark archive link") {
		t.Fatal("could not isolate the named table-sanitizer step")
	}

	reportMode := "current-only"
	invoke := func(t *testing.T, report string, symlink bool, wantSuccess bool) string {
		t.Helper()
		dir := t.TempDir()
		reportPath := filepath.Join(dir, "report.md")
		if symlink {
			target := filepath.Join(dir, "target.md")
			if err := os.WriteFile(target, []byte(report), 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(target, reportPath); err != nil {
				t.Fatal(err)
			}
		} else if err := os.WriteFile(reportPath, []byte(report), 0600); err != nil {
			t.Fatal(err)
		}
		tablePath := filepath.Join(dir, "table.md")
		cmd := exec.Command("bash", "-euo", "pipefail", "-c", script)
		cmd.Env = append(os.Environ(), "REPORT_PATH="+reportPath, "TABLE_PATH="+tablePath, "REPORT_MODE="+reportMode)
		output, err := cmd.CombinedOutput()
		if (err == nil) != wantSuccess {
			t.Fatalf("sanitizer success=%v want %v; output: %s", err == nil, wantSuccess, output)
		}
		if err == nil {
			result, readErr := os.ReadFile(tablePath)
			if readErr != nil {
				t.Fatal(readErr)
			}
			return string(result)
		}
		return ""
	}

	fixturePath := filepath.Join(root, "benchmarks/btp/tests/presentation-current-renderer.md")
	fixture, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatal(err)
	}
	safe := string(fixture)
	result := invoke(t, safe, false, true)
	if !strings.Contains(result, "| Complete lifecycle iteration duration | 19.5 min |") ||
		!strings.Contains(result, "| Mean time until Subaccount is Ready (client-observed) | 500 ms |") ||
		!strings.Contains(result, "| Mean time until Subaccount Kubernetes object is absent (client-observed) | 250 ms |") ||
		strings.Count(result, "\n") != 30 {
		t.Fatalf("renderer-compatible table was not normalized as expected: %q", result)
	}
	reportMode = "comparison"
	comparisonFixturePath := filepath.Join(root, "benchmarks/btp/tests/presentation-comparison-renderer.md")
	comparisonFixture, err := os.ReadFile(comparisonFixturePath)
	if err != nil {
		t.Fatal(err)
	}
	comparison := invoke(t, string(comparisonFixture), false, true)
	for _, expected := range []string{
		"| Complete lifecycle iteration duration | 19.5 min | 20 min | +2.9% |",
		"| Mean time until Subaccount is Ready (client-observed) | 500 ms | 510 ms | +2.0% |",
		"| Provider container maximum sampled CPU usage | 250 millicores | 250 millicores | +0.0% |",
		"| Subaccount observed reconciliation errors | 0 count | 1 count | Unavailable — percentage change requires a strictly positive baseline |",
		"Unavailable — baseline: evidence missing; current: observed",
	} {
		if !strings.Contains(comparison, expected) {
			t.Errorf("comparison table lost renderer value %q: %q", expected, comparison)
		}
	}
	for _, reason := range []string{
		"measurement definition differs between runs", "metric type differs between runs",
		"metric kind differs between runs", "distinct run identity is unavailable",
		"baseline and current identify the same run", "percentage change requires a strictly positive baseline",
		"change unavailable because a side cannot be represented in its display unit",
		"percentage change is outside the finite numeric range",
	} {
		hostile := strings.Replace(string(comparisonFixture), "Unavailable — "+reason, "Unavailable — "+reason+"!", 1)
		invoke(t, hostile, false, false)
	}
	for _, hostile := range []string{
		strings.Replace(string(comparisonFixture), "+4.0%", "Unavailable — made-up reason", 1),
		strings.Replace(string(comparisonFixture), "+4.0%", "Unavailable — metric type differs between runs <b>x</b>", 1),
		"| Measurement | Baseline | Current | Change (%) |\n| --- | ---: | ---: | ---: |\n| X | 1 ms | 2 ms | [link](https://example.com) |\n",
		"| Measurement | Baseline | Current | Change (%) |\n| --- | ---: | ---: | ---: |\n| X | 1 ms | 2 ms | baseline: hostile |\n",
	} {
		invoke(t, hostile, false, false)
	}
	reportMode = "current-only"
	for _, hostile := range []string{
		strings.Replace(safe, `Provider container mean CPU usage`, `Provider \q container mean CPU usage`, 1),
		strings.Replace(safe, `Provider container mean CPU usage`, `<b>unsafe</b>`, 1),
		"| Measurement | Value |\n| --- | ---: |\n| X | [link](https://example.com) |\n",
		"| Measurement | Value |\n| --- | ---: |\n| X | @mention |\n",
		"| Measurement | Value |\n| --- | ---: |\n| X | `code` |\n",
		"| Measurement | Value |\n| --- | ---: |\n| X | <b>html</b> |\n",
		"| Measurement | Value |\n| --- | ---: |\n| X | 1 widgets |\n",
		"| Measurement | Value |\n| --- | ---: |\n| X | 1 | extra |\n",
		"| Measurement | Baseline | Current | Change (%) |\n| --- | ---: | ---: | ---: |\n| X | 1 ms | 2 ms | +1.0% |\n",
	} {
		invoke(t, hostile, false, false)
	}
	invoke(t, safe, true, false)
	invoke(t, safe+strings.Repeat("x", 65537), false, false)
}
