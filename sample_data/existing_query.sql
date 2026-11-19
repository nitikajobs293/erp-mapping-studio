-- Example legacy query: upload as "Existing SQL queries" to harvest join candidates (stored as pending).
SELECT v.VBELN, p.POSNR, l.VBELN AS delivery
FROM VBAK v
JOIN VBAP p ON v.MANDT = p.MANDT AND v.VBELN = p.VBELN
LEFT JOIN LIPS l ON l.MANDT = p.MANDT AND l.VGBEL = p.VBELN AND l.VGPOS = p.POSNR;
