Drop Outlook mail in this folder on the laptop.

incoming/       .msg and .eml files, or a loose PDF / Excel / Word file
                (in Outlook: select the messages and drag them here)
attachments/    optional extra files, in a subfolder named exactly like the message
                example: attachments/Harbor invoice/INV-555.pdf
processed/      originals moved here after a successful read
extracted/      attachments unpacked from each message
failed/         files that could not be read, each with a .why.txt note

Then double-click CloseDesk (or run: python -m controller_inbox run),
or click "Process new mail" on the Today page.
Dropping the same message twice is safe: it is recognised and skipped.
