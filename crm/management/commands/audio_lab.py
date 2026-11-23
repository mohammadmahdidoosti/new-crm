from django.core.management.base import BaseCommand

from crm import lab
from crm.models import Call


def parse_ids(spec: str) -> list[int]:
    ids = []
    for part in (spec or "").replace("،", ",").split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            ids += list(range(int(a), int(b) + 1))
        elif part.isdigit():
            ids.append(int(part))
    return ids


class Command(BaseCommand):
    help = "Transcribe the given calls with cheaper settings and store the results for comparison (calls are not changed)."

    def add_arguments(self, parser):
        parser.add_argument("--calls", required=True, help="e.g. 2-11 or 2,5,7")
        parser.add_argument("--variants", default=",".join(lab.VARIANTS))

    def handle(self, *args, **o):
        variants = [v for v in o["variants"].split(",") if v in lab.VARIANTS]
        data = lab.load()
        total = 0.0
        for call in Call.objects.filter(pk__in=parse_ids(o["calls"])).exclude(audio=""):
            for v in variants:
                try:
                    r = lab.run_one(call, v)
                except Exception as e:  # keep going; one failure should not lose the others
                    r = {"error": str(e)[:300]}
                data.setdefault(str(call.pk), {})[v] = r
                lab.save(data)
                total += r.get("cost", 0)
                self.stdout.write(f"call {call.pk} {v}: {r.get('cost', r.get('error'))}")
        self.stdout.write(f"done, total ${total:.5f}")
