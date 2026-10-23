"""PanDA collection templates shared by the submit kernel and collision guard."""


def output_dataset_name(spec, label):
    return f"{spec['outDS']}_{label}/"


def panda_dataset_names(spec):
    names = [output_dataset_name(spec, name.split('.')[0])
             for name in spec.get('stageOutFiles', [])]
    if not spec.get('noLog'):
        names.append(output_dataset_name(spec, 'log'))
    return sorted(set(names))
