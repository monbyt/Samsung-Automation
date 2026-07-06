"""
Decrypt password-protected Excel files via Excel COM Interop (Windows only).

Samsung mail attachments are often encrypted; pandas cannot read them directly.
This converts the workbook to a plain .xlsx in-place using Excel + openpyxl.
"""
import datetime
import os
import sys
import time

CHUNK_SIZE = 1000


def _strip_tz(dt):
    if isinstance(dt, (datetime.datetime, datetime.time)) and getattr(dt, "tzinfo", None):
        return dt.replace(tzinfo=None)
    return dt


def _normalize_excel_data(data_chunk, rows_count, n_cols):
    if data_chunk is None:
        return tuple()
    if not isinstance(data_chunk, tuple):
        return ((data_chunk,),)
    if rows_count == 1:
        if not isinstance(data_chunk[0], tuple):
            return (data_chunk,)
        return data_chunk
    if n_cols == 1:
        return tuple((v,) for v in data_chunk)
    return data_chunk


def prep_excel(src_path: str) -> str:
    """
    Decrypt / convert *src_path* and replace the original file.
    Returns the path to the readable .xlsx (same location as input).
  """
    if sys.platform != "win32":
        raise RuntimeError("Excel decryption requires Windows + Microsoft Excel.")

    import openpyxl
    import win32com.client

    dst_path = src_path.replace(".xlsx", "-temp.xlsx")
    dst_dir = os.path.dirname(dst_path) or "."
    os.makedirs(dst_dir, exist_ok=True)

    if os.path.exists(dst_path):
        os.remove(dst_path)
        while os.path.exists(dst_path):
            time.sleep(1)

    excel = win32com.client.DispatchEx("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False

    try:
        wb_interop = excel.Workbooks.Open(os.path.abspath(src_path))
        ws_interop = wb_interop.ActiveSheet
        used_range = ws_interop.UsedRange
        n_rows = used_range.Rows.Count
        n_cols = used_range.Columns.Count

        wb_xlsx = openpyxl.Workbook()
        ws_xlsx = wb_xlsx.active

        for row_start in range(1, n_rows + 1, CHUNK_SIZE):
            row_end = min(row_start + CHUNK_SIZE - 1, n_rows)
            top_left = ws_interop.Cells(row_start, 1)
            bottom_right = ws_interop.Cells(row_end, n_cols)
            data_chunk = ws_interop.Range(top_left, bottom_right).Value
            rows_count = row_end - row_start + 1
            data_chunk = _normalize_excel_data(data_chunk, rows_count, n_cols)

            if data_chunk:
                for r_off, row in enumerate(data_chunk):
                    for c_off, value in enumerate(row):
                        ws_xlsx.cell(
                            row=row_start + r_off,
                            column=1 + c_off,
                            value=_strip_tz(value),
                        )

        wb_xlsx.save(dst_path)
        wb_interop.Close(False)
    finally:
        excel.Quit()

    os.remove(src_path)
    final_path = dst_path.replace("-temp.xlsx", ".xlsx")
    os.rename(dst_path, final_path)
    return final_path


def prepare_for_reading(path: str) -> str:
    """
    Ensure *path* is readable by pandas. Tries a quick open first;
    falls back to COM decryption on Windows.
    """
    import pandas as pd

    try:
        pd.read_excel(path, nrows=1)
        return path
    except Exception:
        pass

    if sys.platform != "win32":
        raise RuntimeError(
            f"Cannot read encrypted Excel on this OS: {path}. "
            "Run the monitor on a Windows machine with Excel installed."
        )

    print(f"Decrypting via Excel: {os.path.basename(path)}")
    return prep_excel(path)
