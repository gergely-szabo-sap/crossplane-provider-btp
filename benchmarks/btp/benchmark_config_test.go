package benchmarks

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

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
