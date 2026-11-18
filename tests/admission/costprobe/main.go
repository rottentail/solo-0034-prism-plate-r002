// Command costprobe is an INDEPENDENT admission-evidence harness. It never runs
// inside the service and never changes production limits. Its static accounting
// (flattening and edge-traversal product) is re-implemented here instead of
// importing internal/validate, while the measured render path reuses the
// production raster/vector packages so timing evidence comes from real code.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"prismplate/internal/bundle"
	"prismplate/internal/geom"
	"prismplate/internal/raster"
	"prismplate/internal/repository"
	"prismplate/internal/scene"
	"prismplate/internal/validate"
	"prismplate/internal/vector"
	"time"
)

// productionBudget is copied as an evidence constant; the harness does not
// reference any mutable production knob. It must stay in sync manually and the
// run report states the value it was built against.
const productionBudget = int64(160_000_000)
const samplesPerPixel = 4

type plateRequest struct {
	ID       string         `json:"id"`
	Title    string         `json:"title"`
	Document scene.Document `json:"document"`
}

// flattenSegments is the harness's own account of the subdivision contract:
// Q adds 16 vertices, C adds 24, each ring contributes one closing edge.
func flattenSegments(ops []geom.Op) (segments, contours int) {
	open := false
	ringVerts := 0
	flush := func() {
		if ringVerts >= 3 {
			contours++
			segments += ringVerts
		}
		ringVerts = 0
	}
	for _, o := range ops {
		switch o.Kind {
		case "M":
			flush()
			open = true
			ringVerts = 1
		case "L":
			ringVerts++
		case "Q":
			ringVerts += 16
		case "C":
			ringVerts += 24
		case "Z":
			flush()
			open = false
		}
	}
	if open {
		flush()
	}
	return segments, contours
}

type staticEvidence struct {
	FlattenedSegments int   `json:"flattened_segments"`
	ContourCount      int   `json:"contour_count"`
	SamplesPerPixel   int   `json:"samples_per_pixel"`
	StaticProduct     int64 `json:"static_product"`
	ProductionBudget  int64 `json:"production_budget"`
	IndependentReject bool  `json:"independent_static_reject"`
}

func computeStatic(d scene.Document) staticEvidence {
	ev := staticEvidence{SamplesPerPixel: samplesPerPixel, ProductionBudget: productionBudget}
	add := func(ops []geom.Op) {
		s, c := flattenSegments(ops)
		ev.FlattenedSegments += s
		ev.ContourCount += c
	}
	for _, l := range d.Layers {
		add(l.Clip)
		add(l.Mask)
		for _, s := range l.Shapes {
			add(s.Path)
		}
	}
	ev.StaticProduct = int64(ev.FlattenedSegments) * int64(d.Width) * int64(d.Height) * samplesPerPixel
	ev.IndependentReject = ev.StaticProduct > productionBudget
	return ev
}

// forceCompile builds the production Compiled structure through the real
// flattening routine but WITHOUT the validate.Compile budget gate, so the
// actual cost of a rejected design can still be measured in isolation.
func forceCompile(d scene.Document) (scene.Compiled, error) {
	c := scene.Compiled{Document: d}
	for _, l := range d.Layers {
		cl := scene.CompiledLayer{Layer: l}
		if len(l.Clip) > 0 {
			cl.Clip = geom.Flatten(l.Clip)
		}
		if len(l.Mask) > 0 {
			cl.Mask = geom.Flatten(l.Mask)
		}
		for _, s := range l.Shapes {
			inv, ok := s.Transform.Inverse()
			if !ok {
				return c, errors.New("singular transform while force-compiling")
			}
			cl.Shapes = append(cl.Shapes, scene.CompiledShape{Shape: s, Path: geom.Flatten(s.Path), Inverse: inv})
		}
		c.Layers = append(c.Layers, cl)
	}
	segs := 0
	for _, l := range c.Layers {
		for _, r := range l.Clip {
			segs += len(r)
		}
		for _, r := range l.Mask {
			segs += len(r)
		}
		for _, s := range l.Shapes {
			for _, r := range s.Path {
				segs += len(r)
			}
		}
	}
	c.Segments = segs
	return c, nil
}

type runStats struct {
	OK              bool   `json:"ok"`
	TimedOut        bool   `json:"timed_out"`
	Error           string `json:"error,omitempty"`
	ElapsedMillis   int64  `json:"elapsed_millis"`
	Bytes           int    `json:"bytes,omitempty"`
	SHA256          string `json:"sha256,omitempty"`
	CancelledMillis int64  `json:"cancel_observed_millis"`
}

func measureRaster(ctx context.Context, c scene.Compiled) runStats {
	start := time.Now()
	data, err := raster.Render(ctx, c)
	elapsed := time.Since(start)
	st := runStats{ElapsedMillis: elapsed.Milliseconds(), CancelledMillis: elapsed.Milliseconds()}
	if err != nil {
		st.Error = err.Error()
		if errors.Is(err, context.DeadlineExceeded) {
			st.TimedOut = true
		}
		return st
	}
	st.OK = true
	st.Bytes = len(data)
	st.SHA256 = repository.Hash(data)
	return st
}

func measureSVG(c scene.Compiled) runStats {
	start := time.Now()
	data := vector.Export(c)
	elapsed := time.Since(start)
	return runStats{OK: true, ElapsedMillis: elapsed.Milliseconds(), Bytes: len(data), SHA256: repository.Hash(data)}
}

type report struct {
	Case               string         `json:"case"`
	RequestBytes       int            `json:"request_bytes"`
	Static             staticEvidence `json:"static"`
	ProductionAdmit    bool           `json:"production_compile_admit"`
	ProductionError    string         `json:"production_compile_error,omitempty"`
	ForceSegmentsMatch bool           `json:"force_segments_match_independent"`
	RasterRuns         []runStats     `json:"raster_runs"`
	SVGRuns            []runStats     `json:"svg_runs"`
	GeneratedAt        string         `json:"generated_at_utc"`
	TimeoutMillis      int64          `json:"probe_timeout_millis"`
}

func runMeasure(casePath, outPath string, timeoutMS int64, repeats int) error {
	raw, err := os.ReadFile(casePath)
	if err != nil {
		return err
	}
	var req plateRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return fmt.Errorf("case is not a plate request: %w", err)
	}
	rep := report{Case: casePath, RequestBytes: len(raw), GeneratedAt: time.Now().UTC().Format(time.RFC3339Nano), TimeoutMillis: timeoutMS}
	rep.Static = computeStatic(req.Document)

	// Production decision through the real admission gate (same code path the
	// HTTP service and bundle importer use).
	if _, perr := validate.Compile(req.Document); perr != nil {
		rep.ProductionAdmit = false
		rep.ProductionError = perr.Error()
	} else {
		rep.ProductionAdmit = true
	}

	compiled, ferr := forceCompile(req.Document)
	if ferr != nil {
		return ferr
	}
	rep.ForceSegmentsMatch = compiled.Segments == rep.Static.FlattenedSegments

	for i := 0; i < repeats; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutMS)*time.Millisecond)
		rep.RasterRuns = append(rep.RasterRuns, measureRaster(ctx, compiled))
		cancel()
		rep.SVGRuns = append(rep.SVGRuns, measureSVG(compiled))
	}
	return writeJSON(outPath, rep)
}

func runSeal(casePath, outPath string) error {
	raw, err := os.ReadFile(casePath)
	if err != nil {
		return err
	}
	var req plateRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return err
	}
	docDigest := repository.SceneHash(req.Document)
	p := repository.Plate{
		ID:        req.ID,
		Title:     req.Title,
		State:     "active",
		Revisions: []repository.Revision{{Number: 1, Digest: docDigest, Document: req.Document, Created: time.Now().UTC()}},
	}
	envelope, err := bundle.Seal(p)
	if err != nil {
		return err
	}
	if outPath == "" || outPath == "-" {
		_, err = os.Stdout.Write(envelope)
		return err
	}
	return os.WriteFile(outPath, envelope, 0o600)
}

func writeJSON(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	if path == "" || path == "-" {
		_, err = os.Stdout.Write(b)
		return err
	}
	return os.WriteFile(path, b, 0o600)
}

func main() {
	mode := flag.String("mode", "measure", "measure|seal")
	casePath := flag.String("case", "", "path to a plate-request JSON case")
	out := flag.String("out", "-", "output path (- for stdout)")
	timeoutMS := flag.Int64("timeout-ms", 20000, "per raster-attempt bounded deadline")
	repeats := flag.Int("repeats", 3, "measured render repetitions")
	flag.Parse()
	if *casePath == "" {
		fmt.Fprintln(os.Stderr, "missing -case")
		os.Exit(2)
	}
	var err error
	switch *mode {
	case "measure":
		err = runMeasure(*casePath, *out, *timeoutMS, *repeats)
	case "seal":
		err = runSeal(*casePath, *out)
	default:
		err = fmt.Errorf("unknown mode %q", *mode)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "costprobe:", err)
		os.Exit(1)
	}
}
