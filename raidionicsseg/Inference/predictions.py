import logging
import traceback
import numpy as np
from typing import List
from math import ceil
from copy import deepcopy
from ..Utils.volume_utilities import padding_for_inference, padding_for_inference_both_ends,\
    padding_for_inference_both_ends_patchwise
from ..Utils.configuration_parser import ConfigResources


def run_predictions(data: np.ndarray, model_path: str, parameters: ConfigResources) -> np.ndarray:
    """
    Performs inference using the specified model and TensorFlow as inference engine.

    Parameters
    ----------
    data : np.ndarray
        Pre-processed patient MRI, ready to be used by the inference engine.
    model_path : str
        Filepath where the trained model is stored.
    parameters :  :obj:`ConfigResources`
        Loaded configuration specifying runtime parameters.
    Returns
    -------
    np.ndarray
        Predictions generated by the inference process.
    """
    logging.debug("Loading tensorflow model.")

    import onnxruntime as rt
    providers = ['CPUExecutionProvider']
    model = rt.InferenceSession(model_path, providers=providers)
    model_outputs_specfile = open('.'.join(model_path.split('.')[:-1]) + '_config.txt', 'r')
    model_outputs = model_outputs_specfile.readline().rstrip().replace("'", "").split(',')
    model_outputs = [x.strip() for x in model_outputs]

    final_result = None

    logging.debug("Predicting...")
    if parameters.new_axial_size and len(parameters.new_axial_size) == 3:
        final_result = __run_predictions_whole(data=data, model=model, model_outputs=model_outputs,
                                               deep_supervision=parameters.training_deep_supervision)
    elif parameters.new_axial_size and len(parameters.new_axial_size) == 2:
        final_result = __run_predictions_slabbed(data=data, model=model, model_outputs=model_outputs,
                                                 parameters=parameters,
                                                 deep_supervision=parameters.training_deep_supervision)
    else:
        final_result = __run_predictions_patch(data=data, model=model, model_outputs=model_outputs,
                                               parameters=parameters,
                                               deep_supervision=parameters.training_deep_supervision,)
    return final_result


def __run_predictions_whole(data: np.ndarray, model, model_outputs: List[str],
                            deep_supervision: bool = False) -> np.ndarray:
    """
    Performs inference using the specified model using the whole input at once.

    Parameters
    ----------
    data : np.ndarray
        Pre-processed patient MRI, ready to be used by the inference engine.
    model : obj
        Loaded ONNX model.
    model_outputs: List[str]
        List of output layers name, generated by the ONNX model conversion tool.
    deep_supervision : bool
        Boolean flag to indicate if deep supervision is used in the model.
    Returns
    -------
    np.ndarray
        Predictions generated by the inference process.
    """
    try:
        logging.debug("Starting inference in full volume mode.")
        data_prep = np.expand_dims(data, axis=0)
        data_prep = np.expand_dims(data_prep, axis=-1)
        predictions = model.run(model_outputs, {"input": data_prep})
    except Exception as e:
        logging.error("Following error collected during model inference (whole mode): \n {}".format(traceback.format_exc()))
        raise ValueError("Segmentation inference (whole mode) could not fully proceed.")

    # When running inference with ONNX, the outputs are packed into a list (even if one output only)
    # Can keep the same array indexing as with the deep_supervision flag.
    return predictions[0][0]


def __run_predictions_slabbed(data: np.ndarray, model, model_outputs: List[str], parameters: ConfigResources,
                              deep_supervision: bool = False) -> np.ndarray:
    # @TODO. Have to test with a non deep supervision model with ONNX, to do array indexing always
    try:
        logging.debug("Starting inference in slab-wise mode.")
        slicing_plane = parameters.slicing_plane
        slab_size = parameters.training_slab_size
        new_axial_size = parameters.new_axial_size
        if parameters.swap_training_input:
            tmp = deepcopy(new_axial_size)
            new_axial_size[0] = tmp[1]
            new_axial_size[1] = tmp[0]

        upper_boundary = data.shape[2]
        if slicing_plane == 'sagittal':
            upper_boundary = data.shape[0]
        elif slicing_plane == 'coronal':
            upper_boundary = data.shape[1]

        # Placeholder for the final predictions -- the actual probabilities
        final_result = np.zeros(data.shape + (parameters.training_nb_classes,))
        data = np.expand_dims(data, axis=-1)
        count = 0

        if parameters.predictions_non_overlapping:
            data, pad_value = padding_for_inference(data=data, slab_size=slab_size, slicing_plane=slicing_plane)
            scale = ceil(upper_boundary / slab_size)
            unpad = False
            for chunk in range(scale):
                if chunk == scale-1 and pad_value != 0:
                    unpad = True

                if slicing_plane == 'axial':
                    slab_CT = data[:, :, int(chunk * slab_size):int((chunk + 1) * slab_size), 0]
                elif slicing_plane == 'sagittal':
                    tmp = data[int(chunk * slab_size):int((chunk + 1) * slab_size), :, :, 0]
                    slab_CT = tmp.transpose((1, 2, 0))
                elif slicing_plane == 'coronal':
                    tmp = data[:, int(chunk * slab_size):int((chunk + 1) * slab_size), :, 0]
                    slab_CT = tmp.transpose((0, 2, 1))

                slab_CT = np.expand_dims(np.expand_dims(slab_CT, axis=0), axis=-1)
                if parameters.fix_orientation:
                    slab_CT = np.transpose(slab_CT, axes=(0, 3, 1, 2, 4))
                slab_CT_pred = model.run(model_outputs, {"input": slab_CT})

                if deep_supervision:
                    slab_CT_pred = slab_CT_pred[0]
                if parameters.fix_orientation:
                    slab_CT_pred = np.transpose(slab_CT_pred, axes=(0, 2, 3, 1, 4))

                if not unpad:
                    for c in range(0, slab_CT_pred.shape[-1]):
                        if slicing_plane == 'axial':
                            final_result[:, :, int(chunk * slab_size):int((chunk + 1) * slab_size), c] = \
                                slab_CT_pred[0][:, :, :slab_size, c]
                        elif slicing_plane == 'sagittal':
                            final_result[int(chunk * slab_size):int((chunk + 1) * slab_size), :, :, c] = \
                                slab_CT_pred[0][:, :, :slab_size, c].transpose((2, 0, 1))
                        elif slicing_plane == 'coronal':
                            final_result[:, int(chunk * slab_size):int((chunk + 1) * slab_size), :, c] = \
                                slab_CT_pred[0][:, :, :slab_size, c].transpose((0, 2, 1))
                else:
                    for c in range(0, slab_CT_pred.shape[-1]):
                        if slicing_plane == 'axial':
                            final_result[:, :, int(chunk * slab_size):, c] = \
                                slab_CT_pred[0][:, :, :slab_size-pad_value, c]
                        elif slicing_plane == 'sagittal':
                            final_result[int(chunk * slab_size):, :, :, c] = \
                                slab_CT_pred[0][:, :, :slab_size-pad_value, c].transpose((2, 0, 1))
                        elif slicing_plane == 'coronal':
                            final_result[:, int(chunk * slab_size):, :, c] = \
                                slab_CT_pred[0][:, :, :slab_size-pad_value, c].transpose((0, 2, 1))

                print(count)
                count = count + 1
        else:
            if slab_size == 1:
                for slice in range(0, data.shape[2]):
                    slab_CT = data[:, :, slice, 0]
                    if np.sum(slab_CT > 0.1) == 0:
                        continue
                    slab_CT_pred = model.run(model_outputs, {"input": np.reshape(slab_CT, (1, new_axial_size[0],
                                                                                           new_axial_size[1], 1))})
                    if deep_supervision:
                        slab_CT_pred = slab_CT_pred[0]
                    for c in range(0, slab_CT_pred.shape[-1]):
                        final_result[:, :, slice, c] = slab_CT_pred[:, :, c]
            else:
                #@TODO. Should pad also to make sure all the initial slices have a prediction
                data = padding_for_inference_both_ends(data=data, slab_size=slab_size, slicing_plane=slicing_plane)
                half_slab_size = int(slab_size / 2)
                #for slice in range(half_slab_size, upper_boundary - half_slab_size):
                for slice in range(half_slab_size, upper_boundary):
                    if slicing_plane == 'axial':
                        slab_CT = data[:, :, slice - half_slab_size:slice + half_slab_size, 0]
                    elif slicing_plane == 'sagittal':
                        slab_CT = data[slice - half_slab_size:slice + half_slab_size, :, :, 0]
                        slab_CT = slab_CT.transpose((1, 2, 0))
                    elif slicing_plane == 'coronal':
                        slab_CT = data[:, slice - half_slab_size:slice + half_slab_size, :, 0]
                        slab_CT = slab_CT.transpose((0, 2, 1))

                    slab_CT = np.reshape(slab_CT, (1, new_axial_size[0], new_axial_size[1], slab_size, 1))
                    if np.sum(slab_CT > 0.1) == 0:
                        continue

                    if parameters.fix_orientation:
                        slab_CT = np.transpose(slab_CT, axes=(0, 3, 1, 2, 4))
                    slab_CT_pred = model.run(model_outputs, {"input": slab_CT})
                    if deep_supervision:
                        slab_CT_pred = slab_CT_pred[0]
                    if parameters.fix_orientation:
                        slab_CT_pred = np.transpose(slab_CT_pred, axes=(0, 2, 3, 1, 4))

                    for c in range(0, slab_CT_pred.shape[-1]):
                        if slicing_plane == 'axial':
                            #final_result[:, :, slice, c] = slab_CT_pred[0][:, :, half_slab_size, c]
                            final_result[:, :, slice - half_slab_size, c] = slab_CT_pred[0][:, :, half_slab_size, c]
                        elif slicing_plane == 'sagittal':
                            final_result[slice, :, :, c] = slab_CT_pred[0][:, :, half_slab_size, c]
                        elif slicing_plane == 'coronal':
                            final_result[:, slice, :, c] = slab_CT_pred[0][:, :, half_slab_size, c]

                    print(count)
                    count = count + 1
    except Exception as e:
        logging.error(
            "Following error collected during model inference (slab mode): \n {}".format(traceback.format_exc()))
        raise ValueError("Segmentation inference (slab mode) could not fully proceed.")
    return final_result


def __run_predictions_patch(data: np.ndarray, model, model_outputs: List[str], parameters: ConfigResources,
                            deep_supervision: bool = False) -> np.ndarray:
    try:
        logging.debug("Starting inference in patch-wise mode.")
        patch_size = parameters.training_patch_size
        patch_offset = parameters.training_patch_offset

        # Padding in case the patch size is larger that one of the volume dimensions
        data, extra_dims = padding_for_inference_both_ends_patchwise(data, patch_size)

        # Placeholder for the final predictions -- the actual probabilities
        final_result = np.zeros(data.shape + (parameters.training_nb_classes,))
        data = np.expand_dims(data, axis=-1)

        for x in range(0, int(np.ceil(data.shape[0] / (patch_size[0] - patch_offset[0])))):
            for y in range(0, int(np.ceil(data.shape[1] / (patch_size[1] - patch_offset[1])))):
                for z in range(0, int(np.ceil(data.shape[2] / (patch_size[2] - patch_offset[2])))):
                    # patch_boundaries_x = [x * patch_size[0], (x + 1) * patch_size[0]]
                    # patch_boundaries_y = [y * patch_size[1], (y + 1) * patch_size[1]]
                    # patch_boundaries_z = [z * patch_size[2], (z + 1) * patch_size[2]]
                    patch_boundaries_x = [x * (patch_size[0] - patch_offset[0]), x * (patch_size[0] - patch_offset[0]) + patch_size[0]]
                    patch_boundaries_y = [y * (patch_size[1] - patch_offset[1]), y * (patch_size[1] - patch_offset[1]) + patch_size[1]]
                    patch_boundaries_z = [z * (patch_size[2] - patch_offset[2]), z * (patch_size[2] - patch_offset[2]) + patch_size[2]]

                    if patch_boundaries_x[1] >= data.shape[0]:
                        diff = abs(data.shape[0] - patch_boundaries_x[1])
                        new_patch_boundaries_x = [patch_boundaries_x[0] - diff, patch_boundaries_x[1] - diff]
                        if new_patch_boundaries_x[0] < 0:
                            continue
                        patch_boundaries_x = new_patch_boundaries_x

                    if patch_boundaries_y[1] >= data.shape[1]:
                        diff = abs(data.shape[1] - patch_boundaries_y[1])
                        new_patch_boundaries_y = [patch_boundaries_y[0] - diff, patch_boundaries_y[1] - diff]
                        if new_patch_boundaries_y[0] < 0:
                            continue
                        patch_boundaries_y = new_patch_boundaries_y

                    if patch_boundaries_z[1] >= data.shape[2]:
                        diff = abs(data.shape[2] - patch_boundaries_z[1])
                        new_patch_boundaries_z = [patch_boundaries_z[0] - diff, patch_boundaries_z[1] - diff]
                        if new_patch_boundaries_z[0] < 0:
                            continue
                        patch_boundaries_z = new_patch_boundaries_z

                    patch = data[patch_boundaries_x[0]:patch_boundaries_x[1], patch_boundaries_y[0]:patch_boundaries_y[1],
                            patch_boundaries_z[0]:patch_boundaries_z[1]]
                    model_input = np.expand_dims(patch, axis=0)
                    patch_pred = model.run(model_outputs, {"input": model_input})
                    # @TODO. Have to test with a non deep supervision model with ONNX, to do array indexing always
                    if deep_supervision:
                        patch_pred = patch_pred[0]

                    # In case of overlapping inference, taking the maximum probabilities overall.
                    final_result[patch_boundaries_x[0]:patch_boundaries_x[1],
                    patch_boundaries_y[0]:patch_boundaries_y[1],
                    patch_boundaries_z[0]:patch_boundaries_z[1], :] = np.maximum(patch_pred[0],
                                                                                 final_result[patch_boundaries_x[0]:patch_boundaries_x[1],
                                                                                 patch_boundaries_y[0]:patch_boundaries_y[1],
                                                                                 patch_boundaries_z[0]:patch_boundaries_z[1], :])
        final_result = final_result[extra_dims[0]:final_result.shape[0] - extra_dims[1],
                       extra_dims[2]:final_result.shape[1] - extra_dims[3],
                       extra_dims[4]:final_result.shape[2] - extra_dims[5], :]
    except Exception as e:
        logging.error(
            "Following error collected during model inference (patch mode): \n {}".format(traceback.format_exc()))
        raise ValueError("Segmentation inference (patch mode) could not fully proceed.")
    return final_result
