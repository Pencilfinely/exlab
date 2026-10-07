"""Simulated sequential experiments illustrating the producer contract (no training)."""
import time

from expman.sdk import Run


def main():
    run = Run()
    for index in range(5):
        experiment_id = "simulation-" + str(index)
        run.progress(experiment_id, "preparing", detail="Synthetic acceptance fixture")
        checkpoint = run.output / experiment_id / "best.bin"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_bytes((experiment_id + " synthetic checkpoint").encode())
        evidence = run.evidence(checkpoint, "synthetic-checkpoint")
        run.progress(experiment_id, "test")
        run.publish_result(experiment_id, {
            "status": "succeeded", "execution": {"status": "succeeded", "stages": {
                "training": {"status": "not_run", "exit_code": None},
                "test": {"status": "not_run", "exit_code": None}}},
            "metrics": {"protocol": "synthetic-example-v1", "validation": {"score": 0.2 + index * 0.01},
                        "test": {"score": 0.1 + index * 0.01}},
            "selection": {"checkpoint": evidence["path"], "sha256": evidence["sha256"], "epoch": 0,
                          "rule": "synthetic fixture; no scientific model selection"},
            "provenance": {"data_version": "synthetic-v1"},
            "timing": {"stages": {}, "note": "No scientific training or testing was performed."},
            "verification": {"remote": "unknown", "independent": "pending"}, "evidence": [evidence],
        })
        time.sleep(0.3)  # Earlier results can be delivered while later fixtures are produced.


if __name__ == "__main__":
    main()
