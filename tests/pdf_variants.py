"""Make strictly synthetic shorter/longer/re-encoded PDFs using pypdf only."""
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ByteStringObject


def variant(source, target, mode):
    reader = PdfReader(source)
    writer = PdfWriter(clone_from=reader)
    page = writer.pages[0]
    content = page.get_contents()
    ops = list(content.operations)
    row = next(i for i, (values, operator) in enumerate(ops)
               if operator == b'Tj' and str(values[0]).startswith('02.08.2026') and '12:02' in str(values[0]))
    description = next(i for i in range(row+1,len(ops)) if ops[i][1] == b'Tj')
    fragment = ops[row:description+2]

    def replace_text(item, before, after):
        values, operator = item
        if operator == b'Tj':
            raw = values[0].original_bytes
            return ([ByteStringObject(raw.replace(before,after))], operator)
        return item

    if mode == 'short':
        del ops[row:description+2]
        ops = [replace_text(item,b'150,40',b'125,25') for item in ops]
        ops = [replace_text(item,b'1 349,85',b'1 375,00') if item[1] == b'Tj' and '31.08.2026' in str(item[0][0]) else item for item in ops]
    elif mode == 'extended':
        duplicate = [replace_text(item,b'1 349,85',b'1 324,70') for item in fragment]
        ops[row:row] = duplicate
        ops = [replace_text(item,b'150,40',b'175,55') for item in ops]
        ops = [replace_text(item,b'1 349,85',b'1 324,70') if item[1] == b'Tj' and '31.08.2026' in str(item[0][0]) else item for item in ops]
    elif mode != 'reencoded':
        raise ValueError(mode)
    content.operations = ops
    page.replace_contents(content)
    writer.add_metadata({'/Subject':'Synthetic test variant ' + mode})
    writer.write(target)
