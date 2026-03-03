from rest_framework import serializers
from .models import PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, Dataset, ProdConfig
from .schemas import validate_parameters


class PhysicsCategorySerializer(serializers.ModelSerializer):
    tag_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = PhysicsCategory
        fields = ['digit', 'name', 'description', 'created_by', 'created_at', 'tag_count']
        read_only_fields = ['created_at']


class PhysicsTagSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)

    class Meta:
        model = PhysicsTag
        fields = [
            'id', 'tag_number', 'tag_label', 'category', 'category_name',
            'status', 'description', 'parameters',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'tag_number', 'tag_label', 'status', 'created_at', 'updated_at']

    def validate_parameters(self, value):
        ok, msg = validate_parameters('p', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class _SimpleTagSerializer(serializers.ModelSerializer):
    class Meta:
        fields = [
            'id', 'tag_number', 'tag_label', 'status', 'description',
            'parameters', 'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'tag_number', 'tag_label', 'status', 'created_at', 'updated_at']


class EvgenTagSerializer(_SimpleTagSerializer):
    class Meta(_SimpleTagSerializer.Meta):
        model = EvgenTag

    def validate_parameters(self, value):
        ok, msg = validate_parameters('e', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class SimuTagSerializer(_SimpleTagSerializer):
    class Meta(_SimpleTagSerializer.Meta):
        model = SimuTag

    def validate_parameters(self, value):
        ok, msg = validate_parameters('s', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class RecoTagSerializer(_SimpleTagSerializer):
    class Meta(_SimpleTagSerializer.Meta):
        model = RecoTag

    def validate_parameters(self, value):
        ok, msg = validate_parameters('r', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class DatasetSerializer(serializers.ModelSerializer):
    physics_tag_label = serializers.CharField(source='physics_tag.tag_label', read_only=True)
    evgen_tag_label = serializers.CharField(source='evgen_tag.tag_label', read_only=True)
    simu_tag_label = serializers.CharField(source='simu_tag.tag_label', read_only=True)
    reco_tag_label = serializers.CharField(source='reco_tag.tag_label', read_only=True)

    class Meta:
        model = Dataset
        fields = [
            'id', 'dataset_name', 'scope', 'detector_version', 'detector_config',
            'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag',
            'physics_tag_label', 'evgen_tag_label', 'simu_tag_label', 'reco_tag_label',
            'block_num', 'blocks', 'did', 'file_count', 'data_size',
            'description', 'metadata', 'created_by', 'created_at',
        ]
        read_only_fields = [
            'id', 'dataset_name', 'did', 'block_num', 'blocks',
            'file_count', 'data_size', 'created_at',
        ]


class ProdConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProdConfig
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']
