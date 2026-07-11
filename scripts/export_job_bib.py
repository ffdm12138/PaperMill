"""Generate write/jobs/<job>/tex/references.bib from job-local article metadata.

引用事实来自 ``write/jobs/<job>/article/<paper_number>/*.metadata.json``（由
``prepare-workset --apply`` 复制），不再读取正式库运行时目录。

用法:
  python scripts/export_job_bib.py --job 001_xxx --from-selected   # 从 selected_papers.json（须 confirmed，且须先 prepare-workset --apply）
  python scripts/export_job_bib.py --job 001_xxx --keys k1 k2
"""
import sys
import argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from src.writer.job_manager import JobManager
from src.writer.bib_manager import export_job_bib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--keys", nargs="*", default=[])
    ap.add_argument("--from-selected", action="store_true", default=True,
                    help="从 selected_papers.json 抽取（默认，须 confirmed）")
    args = ap.parse_args()

    jm = JobManager()
    try:
        # keys 为空时 export_job_bib 自动从 confirmed selected_papers 取
        info = export_job_bib(args.job, bib_keys=args.keys or None, jm=jm)
        logger.info(f"导出 {info['count']} 条到 {info['references_bib']}")
        return 0
    except RuntimeError as e:
        logger.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
