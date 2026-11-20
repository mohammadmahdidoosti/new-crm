"""Import an Excel file from the command line: python manage.py import_excel file.xlsx"""
from django.core.management.base import BaseCommand

from crm.importers import import_workbook


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+")

    def handle(self, *args, **opts):
        for p in opts["paths"]:
            with open(p, "rb") as f:
                for r in import_workbook(f.read()):
                    self.stdout.write(r.line() + (f"  خطا: {r.errors}" if r.errors else ""))
