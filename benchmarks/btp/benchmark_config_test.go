package benchmarks

import (
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
		if mean.Match["resource_kind"] != kind || median.Match["resource_kind"] != kind || median.Statistic != "p50" || mean.Unit != "ms" || median.Unit != "ms" {
			t.Errorf("readiness mean/median pairing incorrect for %s", kind)
		}
	}
	if row := presentation.Measurements[4]; row.ID != "iteration-duration-mean" || row.Source != "raw_k6" || row.Metric != "iteration_duration" || row.Match["scenario"] != "create_delete" || row.Statistic != "mean" || row.Unit != "ms" {
		t.Errorf("invalid complete-iteration duration row: %#v", row)
	}
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
			} `yaml:"steps"`
		} `yaml:"jobs"`
	}
	if err := yaml.Unmarshal(content, &workflow); err != nil {
		t.Fatalf("parse benchmark workflow: %v", err)
	}
	var actionCheckout, execution, report bool
	for _, step := range workflow.Jobs["benchmark"].Steps {
		switch {
		case step.Name == "Check out pinned xp-diadromos actions":
			actionCheckout = step.With["repository"] == "gergely-szabo-sap/xp-diadromos" &&
				step.With["ref"] == "986f84848429e28ee64db70af976c6d2a9472b4e"
		case strings.HasSuffix(step.Uses, "/run-test"):
			execution = step.With["version"] == "v0.9.2"
		case strings.HasSuffix(step.Uses, "/metrics-ci-report"):
			report = step.With["version"] == "v0.9.2" && step.With["input"] == "${{ steps.test.outputs.archive }}"
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
	k6Image := config.K6.CustomImage == "ghcr.io/gergely-szabo-sap/xp-diadromos-k6:v0.9.2"
	modeOutput := strings.Contains(workflow.Jobs["benchmark"].Outputs["report_mode"], "baseline-validation") &&
		strings.Contains(workflow.Jobs["benchmark"].Outputs["report_mode"], "current-only")
	workflowText := string(content)
	baselineWiring := strings.Contains(workflowText, "actions: read") &&
		strings.Contains(workflowText, "baseline-artifact.py resolve") &&
		strings.Contains(workflowText, "baseline-artifact.py validate") &&
		strings.Contains(workflowText, "baseline: ${{ steps.baseline-validation.outputs.baseline_path }}") &&
		strings.Contains(workflowText, "baseline-ref.json")
	if !actionCheckout || !execution || !report || !k6Image || !modeOutput || !baselineWiring {
		t.Fatalf("unexpected benchmark tool selection/report mode: checkout=%v execution-v0.9.2=%v report-v0.9.2=%v k6-image-v0.9.2=%v dynamic-mode-output=%v baseline-wiring=%v", actionCheckout, execution, report, k6Image, modeOutput, baselineWiring)
	}
	ref, err := os.ReadFile(filepath.Join(root, "benchmarks/btp/baseline-ref.json"))
	if err != nil || strings.TrimSpace(string(ref)) != `{"schema_version":"v1","baseline":null}` {
		t.Fatalf("initial baseline reference must remain explicitly disabled: err=%v value=%s", err, ref)
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

	rows := []string{"| Measurement | Value |", "| --- | ---: |"}
	for i := 0; i < 28; i++ {
		label := "Measurement " + strings.Repeat("x", i%4)
		value := "1.25 ms"
		if i == 0 {
			label = `Readiness (client-observed)`
			value = "1.419e+06 ms"
		}
		if i == 1 {
			value = "0 count"
		}
		if i == 2 {
			value = "Unavailable — missing evidence"
		}
		rows = append(rows, "| "+label+" | "+value+" |")
	}
	safe := strings.Join(rows, "\n") + "\n"
	result := invoke(t, safe, false, true)
	if !strings.Contains(result, "| Readiness (client-observed) | 1.419e+06 ms |") || strings.Count(result, "\n") != 30 {
		t.Fatalf("safe full table was not rendered as expected: %q", result)
	}
	reportMode = "comparison"
	comparisonRows := []string{"| Measurement | Baseline | Current | Change (%) |", "| --- | ---: | ---: | ---: |"}
	for i := 0; i < 28; i++ {
		change := "+12.3%"
		switch i {
		case 0:
			change = "Unavailable — baseline: zero baseline; current: missing"
		case 1:
			change = "-3.4%"
		case 2:
			change = "+0.0%"
		}
		comparisonRows = append(comparisonRows, "| Measurement "+strings.Repeat("x", i%4)+" | 1 ms | 2 ms | "+change+" |")
	}
	comparison := invoke(t, strings.Join(comparisonRows, "\n")+"\n", false, true)
	if !strings.Contains(comparison, "| Measurement x | 1 ms | 2 ms | +12.3% |") ||
		!strings.Contains(comparison, "| Measurement x | 1 ms | 2 ms | -3.4% |") ||
		!strings.Contains(comparison, "| Measurement xx | 1 ms | 2 ms | +0.0% |") {
		t.Fatalf("comparison table was not preserved: %q", comparison)
	}
	for _, hostile := range []string{
		"| Measurement | Baseline | Current | Change (%) |\n| --- | ---: | ---: | ---: |\n| X | 1 ms | 2 ms | [link](https://example.com) |\n",
		"| Measurement | Baseline | Current | Change (%) |\n| --- | ---: | ---: | ---: |\n| X | 1 ms | 2 ms | baseline: hostile |\n",
	} {
		invoke(t, hostile, false, false)
	}
	reportMode = "current-only"
	for _, hostile := range []string{
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
