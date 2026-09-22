Drop Outlook mail in this folder on the laptop.

incoming/       .msg and .eml files, or a loose PDF / Excel / Word file
attachments/    optional extra files, in a subfolder named exactly like the message
                example: attachments/Harbor invoice/INV-555.pdf
processed/      originals moved here after a successful read
extracted/      attachments unpacked from each message

Read them with:  python -m controller_inbox ingest
or the "Read the drop folder" button on the Setup page.
