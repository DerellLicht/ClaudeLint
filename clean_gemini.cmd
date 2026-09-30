@echo off
rem Wrapper for clean_gemini.py - keeps the .py next to this file.
rem Usage: clean_gemini file1.md [file2.md ...]   (add --inplace to overwrite)
rem python "%~dp0clean_gemini.py" %*
rem DDM: I modified this to *only* work on one file,
rem      since that is how I will typically use it.
rem =============================================================

@if /I "%~2"=="" goto :usage
@if /I "%~1"=="copy" goto :copy
@if /I "%~1"=="apply" goto :apply

:usage
   @echo USAGE:
   @echo     clean_gemini [copy] [apply] ^<filename^>
   @echo.
   @echo ARGUMENTS
   @echo    copy -  clean ^<filename^>, output is ^<filename^> - clean.md
   @echo    apply - clean ^<filename^>, output is ^<filename^>
   @echo.
   @echo    Either copy or apply are required
   @echo    ^<filename^>  [mandatory; filename does include extension here]
   @echo.
   @echo    Example: 
   @echo    clean_gemini copy read_files
   @goto :eof

:copy
   python "%~dp0clean_gemini.py" "%~2"
   @goto :eof

:apply
   python "%~dp0clean_gemini.py" "%~2" --inplace
   @goto :eof

