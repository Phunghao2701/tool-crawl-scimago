-- Seed dữ liệu ranking_metric từ Scimago
INSERT INTO ranking_metric (code, display_name, metric_type, description)
VALUES
    ('RANK',                    'Rank',                      'INTEGER',  'SCImago journal ranking position'),
    ('SJR',                     'SJR Score',                 'SCORE',    'SCImago Journal Rank score'),
    ('SJR_BEST_QUARTILE',       'SJR Best Quartile',         'QUARTILE', 'Best quartile of the journal across all categories'),
    ('SJR_QUARTILE_BY_CAT',     'SJR Quartile By Category',  'QUARTILE', 'Quartile of the journal in a specific category'),
    ('H_INDEX',                 'H Index',                   'INTEGER',  'Journal H-index (all years)'),
    ('TOTAL_DOCS_CURRENT_YEAR', 'Total Docs (Current Year)', 'INTEGER',  'Total documents published in selected year'),
    ('TOTAL_DOCS_3YEARS',       'Total Docs (3 Years)',      'INTEGER',  'Total documents published in last 3 years'),
    ('TOTAL_REFS',              'Total Refs',                'INTEGER',  'Total references'),
    ('TOTAL_CITES_3YEARS',      'Total Cites (3 Years)',     'INTEGER',  'Total citations in last 3 years'),
    ('CITABLE_DOCS_3YEARS',     'Citable Docs (3 Years)',    'INTEGER',  'Citable documents in last 3 years'),
    ('CITES_PER_DOC_2YEARS',    'Cites / Doc (2 Years)',     'SCORE',    'Citation rate per document in 2 years'),
    ('REF_PER_DOC',             'Ref / Doc',                 'SCORE',    'References per document')
ON CONFLICT (code) DO UPDATE
    SET display_name = EXCLUDED.display_name,
        metric_type  = EXCLUDED.metric_type,
        description  = EXCLUDED.description;
