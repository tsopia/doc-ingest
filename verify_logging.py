import logging
import sys
from app.main import _configure_logging

# Re-run config to be sure
_configure_logging()

def verify_logging():
    print("--- Starting Verification ---")
    
    # 1. Test pdfminer warning (Should be suppressed)
    pdf_logger = logging.getLogger("pdfminer.pdfinterp")
    print("Emitting pdfminer WARNING (Expected: NO OUTPUT)")
    pdf_logger.warning("Could not get FontBBox from font descriptor...")
    
    # 2. Test pdfminer error (Should show up)
    print("Emitting pdfminer ERROR (Expected: OUTPUT)")
    pdf_logger.error("This is a real pdfminer error")
    
    # 3. Test generic standard logging (Should show up formatted by loguru)
    print("Emitting generic standard log INFO (Expected: OUTPUT formatted by loguru)")
    logging.getLogger("requests").info("This is a standard library log")

    print("--- Verification Done ---")

if __name__ == "__main__":
    verify_logging()
