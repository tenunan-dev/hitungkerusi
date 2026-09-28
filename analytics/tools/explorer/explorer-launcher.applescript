-- GE16 Graph & Vector Explorer — open the app directly.
--
-- The app is fully file://-compatible (all data loads via <script> tags, no
-- fetch()). A local HTTP server is therefore UNNECESSARY and was the source of
-- repeated bugs (stale browser cache, failed Stop, port TIME_WAIT rebind).
-- This launcher simply opens index.html in the default browser.
on run
    set explorer_dir to POSIX path of (container of (path to me))
    set idx to POSIX file (explorer_dir & "index.html")
    tell application "Finder" to open idx
end run
