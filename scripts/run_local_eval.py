import argparse
import ast
import contextlib
import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import torch
from transformers import AutoTokenizer

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from model.model_lora import apply_lora, load_lora  # noqa: E402
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM  # noqa: E402
from trainer.trainer_utils import get_model_params, setup_seed  # noqa: E402


def load_questions(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_model(args):
    tokenizer = AutoTokenizer.from_pretrained(args.load_from)
    if "model" in args.load_from:
        config = MiniMindConfig(
            hidden_size=args.hidden_size,
            num_hidden_layers=args.num_hidden_layers,
            use_moe=bool(args.use_moe),
            inference_rope_scaling=args.inference_rope_scaling,
        )
        model = MiniMindForCausalLM(config)
        moe_suffix = "_moe" if args.use_moe else ""
        weight_path = Path(args.save_dir) / f"{args.weight}_{args.hidden_size}{moe_suffix}.pth"
        model.load_state_dict(torch.load(weight_path, map_location=args.device), strict=True)
        if args.lora_weight != "None":
            apply_lora(model)
            load_lora(model, str(Path(args.save_dir) / f"{args.lora_weight}_{args.hidden_size}.pth"))
    else:
        from transformers import AutoModelForCausalLM

        model = AutoModelForCausalLM.from_pretrained(args.load_from, trust_remote_code=True)
    get_model_params(model, model.config)
    return model.half().eval().to(args.device), tokenizer


def generate_answer(model, tokenizer, question, args):
    conversation = [{"role": "user", "content": question["prompt"]}]
    if "pretrain" in args.weight:
        prompt = tokenizer.bos_token + question["prompt"]
    else:
        prompt = tokenizer.apply_chat_template(
            conversation,
            tokenize=False,
            add_generation_prompt=True,
            open_thinking=bool(args.open_thinking),
        )
    inputs = tokenizer(prompt, return_tensors="pt", truncation=True).to(args.device)
    setup_seed(args.seed)
    start = time.time()
    with torch.inference_mode():
        generated = model.generate(
            inputs=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            max_new_tokens=args.max_new_tokens,
            do_sample=bool(args.do_sample),
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
            top_p=args.top_p,
            temperature=args.temperature,
            repetition_penalty=args.repetition_penalty,
        )
    answer = tokenizer.decode(generated[0][len(inputs["input_ids"][0]) :], skip_special_tokens=True)
    elapsed = max(time.time() - start, 1e-6)
    new_tokens = len(generated[0]) - len(inputs["input_ids"][0])
    return answer.strip(), new_tokens / elapsed


def extract_python_code(text):
    match = re.search(r"```(?:python)?\s*(.*?)```", text, re.S | re.I)
    if match:
        return match.group(1).strip()
    return text.strip()


def safe_exec_code(code):
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("imports are not allowed")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"open", "exec", "eval", "__import__"}:
            raise ValueError(f"call to {node.func.id} is not allowed")
    safe_builtins = {
        "len": len,
        "range": range,
        "list": list,
        "sum": sum,
        "min": min,
        "max": max,
        "enumerate": enumerate,
        "sorted": sorted,
    }
    namespace = {"__builtins__": safe_builtins}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(tree, "<model_answer>", "exec"), namespace, namespace)
    return namespace


def repetition_penalty(text):
    chars = [c for c in text if not c.isspace()]
    if len(chars) < 20:
        return 0.0
    chunks = ["".join(chars[i : i + 6]) for i in range(max(len(chars) - 5, 0))]
    if not chunks:
        return 0.0
    repeated = len(chunks) - len(set(chunks))
    return repeated / len(chunks)


def score_text(question, answer):
    score = 0.0
    reasons = []
    must = question.get("must_contain", [])
    if must:
        hits = sum(1 for kw in must if kw in answer)
        score += 6.0 * hits / len(must)
        reasons.append(f"must={hits}/{len(must)}")
    else:
        score += 4.0
    avoid_hits = [kw for kw in question.get("avoid", []) if kw and kw in answer]
    if avoid_hits:
        score -= min(3.0, 1.5 * len(avoid_hits))
        reasons.append("avoid=" + ",".join(avoid_hits))
    max_chars = question.get("max_chars")
    if max_chars:
        if len(answer) <= max_chars:
            score += 2.0
            reasons.append("length=ok")
        else:
            overflow = len(answer) - max_chars
            score += max(0.0, 2.0 - overflow / max(max_chars, 1) * 2.0)
            reasons.append(f"length={len(answer)}>{max_chars}")
    rep = repetition_penalty(answer)
    if rep < 0.08:
        score += 2.0
        reasons.append("repeat=ok")
    else:
        score += max(0.0, 2.0 - rep * 10)
        reasons.append(f"repeat={rep:.2f}")
    return max(0.0, min(10.0, score)), reasons


def score_code(question, answer):
    code = extract_python_code(answer)
    reasons = []
    try:
        namespace = safe_exec_code(code)
        fn = namespace.get(question["function"])
        if not callable(fn):
            return 0.0, [f"missing function {question['function']}"]
        passed = 0
        for test in question.get("tests", []):
            got = fn(*test["args"])
            if got == test["expected"]:
                passed += 1
            else:
                reasons.append(f"got {got!r}, expected {test['expected']!r}")
        total = len(question.get("tests", []))
        score = 10.0 * passed / max(total, 1)
        reasons.insert(0, f"tests={passed}/{total}")
        return score, reasons
    except Exception as exc:
        return 0.0, [f"error={type(exc).__name__}: {exc}"]


def score_answer(question, answer):
    if question.get("category") == "code":
        return score_code(question, answer)
    return score_text(question, answer)


def write_report(results, summary, out_path):
    lines = [
        f"# Local Eval Report: {summary['model_name']}",
        "",
        f"- Average score: {summary['average']:.2f}/10",
        f"- Average speed: {summary['avg_speed']:.2f} tokens/s",
        "",
        "## Category Scores",
        "",
    ]
    for category, value in sorted(summary["category_average"].items()):
        lines.append(f"- {category}: {value:.2f}/10")
    lines.extend(["", "## Questions", ""])
    for item in results:
        lines.extend(
            [
                f"### {item['id']} ({item['category']})",
                "",
                f"- Score: {item['score']:.2f}/10",
                f"- Speed: {item['speed']:.2f} tokens/s",
                f"- Reasons: {', '.join(item['reasons'])}",
                "",
                "**Prompt**",
                "",
                item["prompt"],
                "",
                "**Answer**",
                "",
                item["answer"],
                "",
            ]
        )
    out_path.write_text("\n".join(lines), encoding="utf-8")


def summarize(results, model_name):
    by_category = defaultdict(list)
    for item in results:
        by_category[item["category"]].append(item["score"])
    category_average = {k: sum(v) / len(v) for k, v in by_category.items()}
    return {
        "model_name": model_name,
        "average": sum(item["score"] for item in results) / len(results),
        "avg_speed": sum(item["speed"] for item in results) / len(results),
        "category_average": category_average,
    }


def main():
    parser = argparse.ArgumentParser(description="Run a small deterministic local eval for MiniMind torch weights.")
    parser.add_argument("--eval_file", default="evals/local_eval_set.jsonl")
    parser.add_argument("--output_dir", default="evals/results")
    parser.add_argument("--model_name", default=None)
    parser.add_argument("--load_from", default="model")
    parser.add_argument("--save_dir", default="out")
    parser.add_argument("--weight", default="full_sft")
    parser.add_argument("--lora_weight", default="None")
    parser.add_argument("--hidden_size", default=768, type=int)
    parser.add_argument("--num_hidden_layers", default=8, type=int)
    parser.add_argument("--use_moe", default=0, type=int, choices=[0, 1])
    parser.add_argument("--inference_rope_scaling", default=False, action="store_true")
    parser.add_argument("--max_new_tokens", default=256, type=int)
    parser.add_argument("--temperature", default=0.7, type=float)
    parser.add_argument("--top_p", default=0.9, type=float)
    parser.add_argument("--repetition_penalty", default=1.05, type=float)
    parser.add_argument("--do_sample", default=0, type=int, choices=[0, 1])
    parser.add_argument("--open_thinking", default=0, type=int)
    parser.add_argument("--seed", default=42, type=int)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = parser.parse_args()

    questions = load_questions(args.eval_file)
    model, tokenizer = load_model(args)
    model_name = args.model_name or f"{args.weight}_{args.hidden_size}{'_moe' if args.use_moe else ''}"
    results = []
    for i, question in enumerate(questions, 1):
        answer, speed = generate_answer(model, tokenizer, question, args)
        score, reasons = score_answer(question, answer)
        result = {
            "id": question["id"],
            "category": question["category"],
            "prompt": question["prompt"],
            "answer": answer,
            "score": score,
            "reasons": reasons,
            "speed": speed,
        }
        results.append(result)
        print(f"[{i:02d}/{len(questions)}] {question['id']} score={score:.2f} speed={speed:.2f}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(results, model_name)
    json_path = out_dir / f"{model_name}.json"
    md_path = out_dir / f"{model_name}.md"
    json_path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(results, summary, md_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"saved: {json_path}")
    print(f"saved: {md_path}")


if __name__ == "__main__":
    main()
